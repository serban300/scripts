#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import logging
import traceback
import json
import io
from urllib.parse import urlparse
from threading import Lock

from flask import Flask
from waitress import serve
from apscheduler.schedulers.background import BackgroundScheduler
from prometheus_client import generate_latest, Gauge
from websocket import create_connection
from environs import Env

LOGGING_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

ws_metrics = {
    'polkadot_ws_alive': Gauge('wss_alive', 'WebSocket alive', ['url'])
}

# global variable to keep current readiness status
# we need threading.Lock() to avoid race conditions
# some Python modules can use threads inside
readiness_status = {'status': False, 'lock': Lock()}

# Flask app
app = Flask(__name__, static_folder=None)

app_config = {
    'log_level': 'INFO',
    'host': '0.0.0.0',
    'port': 8001,
    'ws_check_interval': 10,
    'ws_timeout': 60,
    'node_rpc_urls': ['ws://127.0.0.1:5556'],
    'node_max_unsynchronized_block_drift': 0,
    'node_min_peers': 10,
}


def read_readiness_status():
    with readiness_status['lock']:
        return readiness_status['status']


def write_readiness_status(status):
    with readiness_status['lock']:
        readiness_status['status'] = status


def uri_validator(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:
        return False


def run_error(msg):
    print('fatal run error! ' + msg, file=sys.stderr)
    sys.exit(1)


def parse_config(config):
    env = Env()
    # WSHE_LOG_LEVEL
    # it checks debug CLI flag to keep backward compatibility with old versions
    if len(sys.argv) > 1 and sys.argv[1] == 'debug':
        config['log_level'] = 'DEBUG'
    else:
        config['log_level'] = env.str("WSHE_LOG_LEVEL", config['log_level']).upper()
        if config['log_level'] not in ['INFO', 'DEBUG']:
            run_error(f'{config["log_level"]} isn\'t a valid log level. It can be INFO or DEBUG')
    # WSHE_HOST
    config['host'] = env.str("WSHE_HOST", config['host'])
    # WSHE_PORT
    # it checks PORT to keep backward compatibility with old versions
    config['port'] = env.int("WSHE_PORT", env.int("PORT", config['port']))
    if config["port"] < 1 or config["port"] > 65535:
        run_error(f'{config["port"]} isn\'t a valid port number')
    # WSHE_WS_CHECK_INTERVAL
    config['ws_check_interval'] = env.int("WSHE_WS_CHECK_INTERVAL", config['ws_check_interval'])
    # WSHE_WS_TIMEOUT
    config['ws_timeout'] = env.int("WSHE_WS_TIMEOUT", config['ws_timeout'])
    # WSHE_NODE_RPC_URLS
    # it checks NODE_URL to keep backward compatibility with old versions
    config['node_rpc_urls'] = env.list("WSHE_NODE_RPC_URLS", env.list("NODE_URL", config['node_rpc_urls']))
    invalid_urls = []
    # validate URLs
    for url in config['node_rpc_urls']:
        if not uri_validator(url):
            invalid_urls.append(url)
    if invalid_urls:
        run_error(f'{invalid_urls} URLs aren\'t valid')
    # WSHE_NODE_MAX_UNSYNCHRONIZED_BLOCK_DRIFT
    config['node_max_unsynchronized_block_drift'] = env.int("WSHE_NODE_MAX_UNSYNCHRONIZED_BLOCK_DRIFT",
                                                            config['node_max_unsynchronized_block_drift'])
    # WSHE_NODE_MIN_PEERS
    config['node_min_peers'] = env.int("WSHE_NODE_MIN_PEERS", config['node_min_peers'])

    print('config:')
    for config_line in sorted(config.items()):
        print(f' {config_line[0]}: {config_line[1]}')


def check_ws(node_url, ws_timeout, node_max_unsynchronized_block_drift, node_min_peers):
    node_state = {'health_summary': True}
    try:
        ws = create_connection(node_url, timeout=ws_timeout)
        ws.send('{"id":1, "jsonrpc":"2.0", "method": "system_health", "params":[]}')
        hc_data = json.loads(ws.recv())
        ws.send('{"id":1, "jsonrpc":"2.0", "method": "system_syncState", "params":[false]}')
        sync_data = json.loads(ws.recv())
        ws.close()
        node_state['is_syncing'] = hc_data['result']['isSyncing']
        node_state['health_summary'] = node_state['health_summary'] and hc_data['result']['isSyncing'] is False
        node_state['peers'] = hc_data['result']['peers']
        node_state['health_summary'] = node_state['health_summary'] and node_state['peers'] >= node_min_peers
        node_state['should_have_peers'] = hc_data['result']['shouldHavePeers']
        node_state['highestBlock'] = sync_data['result']['highestBlock']
        node_state['currentBlock'] = sync_data['result']['currentBlock']
        node_state['unsynchronized_block_drift'] = node_state['highestBlock']  - \
                                                   node_state['currentBlock']
        if node_max_unsynchronized_block_drift > 0:
            node_state['health_summary'] = node_state['health_summary'] and \
                                           node_state['unsynchronized_block_drift'] <= node_max_unsynchronized_block_drift
        logging.debug(f'WebSocket check. URL: {node_url} state: {node_state}')
        return node_state['health_summary']
    except Exception as e:
        logging.error(f'WebSocket request error. URL: {node_url}, timeout: {ws_timeout}, error: "{e}"')
        tb_output = io.StringIO()
        traceback.print_tb(e.__traceback__, file=tb_output)
        logging.debug(f'WebSocket request error. URL: {node_url}, timeout: {ws_timeout}, '
                      f'traceback:\n{tb_output.getvalue()}')
        tb_output.close()
        return False


def update_metrics():
    if not app_config['node_rpc_urls']:
        return
    # the common status will be negative if at least one check for a URL fails
    status = True
    for url in app_config['node_rpc_urls']:
        url_probe = check_ws(node_url=url,
                             ws_timeout=app_config['ws_timeout'],
                             node_max_unsynchronized_block_drift=app_config['node_max_unsynchronized_block_drift'],
                             node_min_peers=app_config['node_min_peers'])
        ws_metrics['polkadot_ws_alive'].labels(url=url).set(int(url_probe))
        status = status and url_probe
    write_readiness_status(status)


@app.route('/')
def site_map():
    routes = "Main Page:\n"
    for rule in app.url_map.iter_rules():
        routes += ('%s\n' % rule)
    return routes


@app.route('/metrics')
def metrics():
    return generate_latest()


@app.route('/health/readiness')
def health_readiness():
    if read_readiness_status():
        return '{"status": "UP"}', 200
    else:
        return '{"status": "DOWN"}', 500


if __name__ == '__main__':
    parse_config(app_config)

    # set up console log handler
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, app_config['log_level']))
    formatter = logging.Formatter(LOGGING_FORMAT)
    console.setFormatter(formatter)
    # set up basic logging config
    logging.basicConfig(format=LOGGING_FORMAT, level=getattr(logging, app_config['log_level']), handlers=[console])

    update_metrics()
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=update_metrics, trigger="interval", seconds=app_config['ws_check_interval'])
    scheduler.start()

    serve(app, host=app_config['host'], port=app_config['port'])
