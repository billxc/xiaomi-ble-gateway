# Windows 被动 BLE 接收端

这是本地脱敏公开候选，不代表已发布或完成 Windows 实机验收。

```text
Windows Python / WinRT 被动监听
  → Noise 加密 ESPHome Native API 1.19 / RAW 广播
  → Home Assistant 原生 ESPHome 集成 / Bluetooth 管理层
  → 仓库根 HA 插件 xiaomi_ble_gateway 的唯一业务 runtime
```

Windows 自行实现 Native API 的广播服务端子集；**不是把客户端 SDK 当服务端运行，也不是真正的 ESP 固件**。多个接收节点只是 HA 的接收源，不应各建一个业务 runtime。解码、业务去重、持久化和云上报由 HA 插件统一负责。

## 能力与边界

- WinRT `PASSIVE`，转发所有设备的 **OS 可见 AD**，不按厂商、MAC、型号、UUID 或解密结果过滤。原始 DataSections 保留顺序、重复项和未知类型；Windows 已解析元数据仅补缺失字段。
- OS 可能合并或丢弃广播；这不是 RF/IQ 抓包，不能恢复 OS 不提供的数据。RSSI `-127` 是 Windows 合成失联事件，仅计数，不转发为真实 RF 广播。
- TCP 默认 `16053`；强制 `Noise_NNpsk0_25519_ChaChaPoly_SHA256`，每节点独立 32 字节 PSK，无明文广播旁路。握手中的节点名称/MAC 等发现元数据不是秘密。
- API 1.19，RAW 消息 93，能力仅 `PASSIVE_SCAN | RAW_ADVERTISEMENTS = 0x21`。支持 HA 初始化、能力查询、空实体列表、Ping 和 RAW 订阅。
- **不支持 GATT、配对、主动连接、OTA、远程执行或固件刷写。**
- 有界线程转交和会话队列，溢出计数；断线清队列，不在重连后回灌旧广播。Windows 无长期逐包 payload 日志。
- 接收端没有米家账号、bindkey 或 Windows cloud sender；不解密 MiBeacon、不做云上报。Noise PSK 是独立传输密钥，不是设备 bindkey。

## 文件与依赖

- `main.py`：生命周期、线程到 asyncio 转交、`status.json`、滚动 `receiver.log`。
- `winrt_capture.py` / `advertisement.py`：WinRT 被动监听及纯 AD 编码。
- `native_api.py`：加密 Native API 广播子集。
- `protocol/`：官方生成的 protobuf schema，来源及哈希见 `SOURCE.json`。
- `requirements.txt` / `requirements-lock.txt`：运行时依赖及 Windows 固定版本锁。客户端 `aioesphomeapi` **仅用于离线互操作测试**；运行时复用官方 protobuf，不依赖客户端 SDK 或其 Cython client/DLL。
- `ecosystem.config.cjs`：可选 PM2 配置，假定其所在安装根目录下有 `src/`、`venv/`、`config.json`。不要直接在源码目录运行该 ecosystem。

要求 Windows、启用且支持 BLE 的默认适配器、Python 3.12+。锁定的 PyWinRT 4 包需要对应 Python/架构的 wheel；缺少 wheel 时安装会失败，不回退源码编译。离线测试可在 Linux 上运行，且不会加载真实 WinRT。

## 安装（用户自行执行）

以下只描述新安装；不迁移现有服务、计划任务或 PM2 守护进程。选择源码仓库以外的专用安装目录，不使用已有私有数据目录。需要写入 ProgramData 时，以目标运行账号提升权限执行；以后也由该账号运行程序。

在本目录打开 PowerShell：

```powershell
$PythonBase = Read-Host 'Python 3.12+ python.exe 的完整路径'
$Root = Join-Path $env:ProgramData 'WindowsBLEProxy'
.\install-runtime.ps1 -PythonBase $PythonBase -Root $Root
```

`PythonBase` 必填，`Root` 可配；可用 `-Uv` 指定 uv，默认查找当前用户的 `.local\bin\uv.exe`，没有时使用 Python venv/pip。脚本创建独立 venv，按锁文件安装依赖；uv 使用 sync，pip 回退按固定版本安装（不会清除既存额外包，推荐全新安装）。

安装根 `privateRoot` 的 ACL 禁止继承，仅授予安装用户、SYSTEM、Administrators，不包含 Everyone。脚本只复制明确列举的业务源码、protocol `.py`/LICENSE/SOURCE 和依赖文件，不递归复制 protocol，不带缓存；ecosystem 和示例配置复制到安装根。脚本不会生成密钥、启动采集、设置 PM2 或修改防火墙。对已有带安装标记目录再次安装会覆盖代码/示例，但不覆盖 `config.json`；不应当把它用作在线热升级脚本。

### 本地生成节点身份及 PSK

`config.example.json` 的 PSK 是**无效占位符**，不能直接启动。为每个节点选择稳定且唯一的名称（1–63 位小写字母、数字、连字符，首字符不能是连字符）。不要使用测试 PSK 或复制其他节点的配置。

以下命令从示例生成真实配置，直接写入受保护的安装目录，不将 key 输出到终端、命令参数或日志；已有配置会拒绝覆盖。不要在录屏、调试追踪或会转储变量的环境中处理密钥。

```powershell
$Node = Read-Host '唯一节点名称，例如 windows-ble-receiver-1'
@'
import base64, json, re, secrets, sys
from pathlib import Path
root, name = Path(sys.argv[1]), sys.argv[2]
if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
    raise SystemExit("Invalid node name")
config = json.loads((root / "config.example.json").read_text(encoding="utf-8-sig"))
config.update(name=name, friendly_name=name,
              noise_psk=base64.b64encode(secrets.token_bytes(32)).decode("ascii"))
with (root / "config.json").open("x", encoding="utf-8") as output:
    json.dump(config, output, indent=2)
    output.write("\n")
'@ | & "$Root\venv\Scripts\python.exe" - $Root $Node
if ($LASTEXITCODE -ne 0) { throw 'Configuration creation failed' }
```

用本地可信编辑器私下读取 `config.json`，将 PSK 填入 HA 原生 ESPHome 集成的加密密钥字段；不要使用打印密钥的命令，不把配置提交到 Git。HA 手动添加此 Windows 主机及端口 `16053`，不需要刷写固件。根 HA 插件只保留一个业务 runtime。

### 防火墙与前台启动

先确定 Windows 网络为 Private，并取得 HA 的实际单一 IP。以下防火墙步骤需要提升权限：

```powershell
$HAAddress = Read-Host 'Home Assistant 实际 IP（单一地址）'
.\register-firewall.ps1 -HomeAssistantAddress $HAAddress
& "$Root\venv\Scripts\python.exe" "$Root\src\main.py" --config "$Root\config.json"
```

规则仅允许 **Private profile + 指定 HA IP + TCP 16053**，不接受 Any/网段，不关闭防火墙、不替换已有规则。HA 地址改变时先检查已有规则，再由管理员有意调整。自定义 API 端口时需单独审查等价的受限防火墙规则；此脚本固定 16053。

### 可选 PM2：复用实际 PM2_HOME

PM2 需另行安装。若已有 PM2，必须确认现有 daemon 的运行账号和实际 `PM2_HOME`，使用同一账号及目录。**不要猜目录、硬编码机器路径，或在新的 SSH/管理员环境里裸运行 pm2**，否则可能启动第二个 daemon。下列输入必须来自用户已核实的配置，而不是新建路径：

```powershell
$env:PM2_HOME = Read-Host '现有 PM2 daemon 已核实的实际 PM2_HOME 完整路径'
if (-not $env:PM2_HOME -or -not (Test-Path -LiteralPath $env:PM2_HOME -PathType Container)) {
    throw '先确认现有 PM2_HOME，不创建另一个 daemon'
}
# 仅在确认账号/PM2_HOME 正确后执行；先检查是否已有同名应用。
pm2 list
pm2 start "$Root\ecosystem.config.cjs" --only windows-ble-proxy
```

ecosystem 的应用名为 `windows-ble-proxy`，不影响其他应用；若同名已存在，先检查其路径，不盲目覆盖。此候选不提供本机 PM2 管理/持久化或注册计划任务脚本。开机恢复沿用用户已有机制；`pm2 save` 会保存整个实时进程表，不能作为“只持久化本应用”的通用步骤。不要重启其他应用或 daemon。

## 状态、隐私与离线测试

PM2 online 不等于正在收包。私下检查安装根 `status.json` 的 state、更新时间及 received/emitted、无订阅、队列丢弃、错误等计数；`receiver.log` 为约 1 MB × 4 的轮转日志。状态/日志可能含适配器名称、MAC、节点名和本机路径，PM2 日志、安装标记也属于本地运行产物；不要公开这些文件、真实配置或 HA diagnostics。

在独立测试环境安装运行时纯 Python 依赖和测试专用客户端（无需 WinRT/硬件），然后从 `receivers/windows` 执行：

```sh
python -m pip install -r requirements.txt aioesphomeapi==46.22.0
python -B -m unittest discover -s tests -v
```

测试在 Linux 上仅使用合成数据、mock WinRT 和 `127.0.0.1` 临时端口，无 HA、外网、真实硬件或抓包环境变量。Windows 上 `requirements.txt` 会额外安装 WinRT，但测试本身仍 mock 硬件。互操作测试中的 SDK 是客户端，仅用于检验 Noise、初始化、RAW、断线、错误 PSK、队列边界等行为。合成测试名称/MAC/PSK 均非部署配置，不能作生产默认值。离线通过不等于已验证真实 Windows 适配器、HA 接入或开机恢复。

## Third-party 与许可

`protocol/api_pb2.py` 和 `protocol/api_options_pb2.py` 是来自 [esphome/aioesphomeapi](https://github.com/esphome/aioesphomeapi) **v46.22.0** 的官方生成 schema，不是本项目原创协议定义；未修改生成文件。`protocol/SOURCE.json` 记录上游 URL、tag、文件路径及 SHA-256，`protocol/LICENSE` 保留 MIT 文本及 Otto Winter 版权声明。候选不包含反编译的厂商插件。

本目录项目代码遵循仓库根 `LICENSE`，上游 protocol 同时遵循 `protocol/LICENSE`，不另选额外许可证。根项目许可证及仓库发布目标尚待用户正式确认（MIT 仅为建议）；确认前本目录仅作为本地候选，不据此宣称已获准发布。
