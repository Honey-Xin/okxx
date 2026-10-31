# 复制为 set_env.ps1 后改值，再 `. .\set_env.ps1` 加载
$env:OKX_API_KEY      = "your-api-key"
$env:OKX_API_SECRET   = "your-secret"
$env:OKX_API_PASSWORD = "your-passphrase"

# 1 = OKX 模拟盘
$env:OKX_SANDBOX = "1"
# 1 = 只打印不下单（默认安全）
$env:DRY_RUN = "1"

# 本机 OKX 域名被污染，必须走代理
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
$env:HTTP_PROXY  = "http://127.0.0.1:7890"

# webhook
$env:WEBHOOK_SECRET = "change-me"
$env:MAX_SIZE       = "0.05"
