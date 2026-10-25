const path = require('path');
const root = __dirname;
module.exports = {
  apps: [{
    name: 'windows-ble-proxy',
    script: path.join(root, 'src', 'main.py'),
    interpreter: path.join(root, 'venv', 'Scripts', 'python.exe'),
    interpreter_args: '-u',
    args: ['--config', path.join(root, 'config.json')],
    cwd: path.join(root, 'src'),
    exec_mode: 'fork',
    instances: 1,
    windowsHide: true,
    autorestart: true,
    restart_delay: 5000,
    min_uptime: 10000,
    max_restarts: 10,
    watch: false,
    out_file: path.join(root, 'pm2-out.log'),
    error_file: path.join(root, 'pm2-error.log'),
    time: true,
    env: { PYTHONUNBUFFERED: '1' },
  }],
};
