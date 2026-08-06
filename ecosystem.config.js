// pm2 配置：Polymarket 交易 bot（TG 交互 + 通知 + 可选 bridge）
// 启动: pm2 start ecosystem.config.js && pm2 save
// 查看: pm2 logs poly-strategy | pm2 monit
module.exports = {
  apps: [
    {
      name: "poly-strategy",
      cwd: "/Users/tony/Workspace/polymarket_trading_bot_strategy",
      script: "main.py",
      interpreter: "/Users/tony/Workspace/polymarket_trading_bot_strategy/.venv/bin/python",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 10,
      kill_timeout: 10000,
      time: true,
    },
  ],
};
