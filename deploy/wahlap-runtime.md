# 华立取分运行环境

网站通过私有 Unix socket 把一次玩家二维码交给独立服务，再由该服务启动沙箱工作进程，从华立读取成绩。取分服务不调用水鱼、不写本站数据库；后端只有在整份成绩通过校验后才导入。水鱼上传是另一项显式操作。

## 来源与分发边界

外部依赖固定为 [AstrBot updater 4aa10aea](https://github.com/XiaoLan9999/astrbot_plugin_maimai_updater/tree/4aa10aea4e559b53e939d9013bde419a33417067) 的 `maimai_official_interface`。固定 SHA-256 见 [wahlap-runtime.lock.json](wahlap-runtime.lock.json)。取分只使用该独立接口层，不安装或运行 AstrBot，也不使用 maimai-ffi 的缺标志成绩路径。

该提交没有 LICENSE/COPYING/NOTICE，`core.dat` 是 Windows 二进制，不能称作完全开源依赖。本仓库不复制其源文件或二进制、不将其打入发布包；部署时从固定上游提交下载到私有的外部目录。后续要分发运行时，应先取得上游许可。运行时下载和依赖安装不访问任何游戏账号。

## 一次性准备

要求 Linux x86_64、Python 3.12+、Wine x64、bubblewrap。当前适配器不提供绕过沙箱的降级。Wine 版本能否加载上游 DLL 必须单独验证；安装成功不等于账号链路已验证。

```sh
python3 scripts/setup_wahlap_runtime.py --runtime-dir /opt/mai-journal-wahlap
install -d -m 700 -o maimai -g maimai /srv/mai-journal/shared/runtime/wahlap-worker
```

运行库及 `.venv` 由 root 拥有，应用用户只读。只有专用状态目录交给应用用户写入，保存 Wine prefix 和进程锁。不要把整个应用 runtime 目录设为 `MAIMAI_WAHLAP_STATE_DIR`。运行库、状态目录和 Wine 路径由下面的取分服务单元配置，不需要放入主应用环境。

安装 socket 和按连接启动的服务单元（新版本应用发布后才可连接）：

```sh
install -m 644 deploy/mai-journal-wahlap.socket /etc/systemd/system/mai-journal-wahlap.socket
install -m 644 deploy/mai-journal-wahlap@.service /etc/systemd/system/mai-journal-wahlap@.service
systemctl daemon-reload
systemctl enable --now mai-journal-wahlap.socket
```

主应用 `/etc/mai-journal/app.env` 仅增加：

```dotenv
MAIMAI_WAHLAP_ENABLED=1
MAIMAI_WAHLAP_SOCKET=/run/mai-journal-wahlap.sock
```

socket 为 root 管理的 `/run` 下文件，仅 `maimai` 用户可读写，权限 0600。每条连接的实例使用 `/srv/mai-journal/current` 对应版本，主应用正常部署无需单独重启常驻取分程序。实例不读取主应用 EnvironmentFile，禁止 core dump，内存上限 1 GiB、交换内存上限 1 GiB，整个请求最长 200 秒。应用内成绩任务限一次并发，跨进程另有文件锁。

在已加载上述主应用环境、以应用用户运行的 Python 中执行 `from backend import wahlap; print(wahlap.available())`，可通过 socket 检查依赖及 Python 沙箱。此检查不提交二维码、不创建游戏会话。

下面是服务内部无凭据探测代码，需放在与 `mai-journal-wahlap@.service` 相同的独立 systemd 限制及静态运行库环境中执行：

```sh
python3 - <<'PY'
from backend import wahlap
runtime, state, wine, bwrap = wahlap._configured_paths()
command = wahlap._sandbox_command(runtime, state, wine, bwrap) + ['--probe']
print(wahlap._run_worker(command, {}, timeout=20))
PY
```

不要在主应用服务中直接启动沙箱。主应用保持 `ProtectKernelTunables=yes`；独立取分服务使用 `ProtectKernelTunables=no`，否则其遮蔽的宿主 `/proc` 会阻止非特权 user namespace 挂载独立 `/proc`。其余服务硬化保持启用，沙箱内不挂载宿主 `/proc`。`--probe` 不加载 DLL 或访问账号；它不能证明 Wine、华立网络或二维码授权已可用。后续有用户授权时才进行账号验证，用户需先结束游戏。

把示例中的 `--probe` 换为 `--probe-runtime`、timeout 改为 60，可在沙箱内测试 Wine 加载 DLL 并检查所需导出函数。这仍不提交二维码或创建玩家会话；首次 Wine prefix 初始化可能较慢。返回 `probe: dll_exports` 才代表这个检查通过。

## 会话与数据校验

1. 二维码仅通过私有 socket 请求体和工作进程 stdin 传递，不放命令参数、环境变量、响应、日志或本站文件。请求最多 4096 字节，响应最多 8 MiB，超时和错误仅返回固定文案。
2. 先读取并校验无需账号的华立游戏配置，网络失败时不提交二维码。随后调用上游解析本次二维码，取得一次会话。读取 preview；只有 `errorId=0` 且明确 `isLogin=false/0` 才发起登录。缺失或未知状态停止；已在游戏中停止。
3. 仅接受登录返回码 1。不接受语义不明的 100，不强制退出已有游戏。不在超时后自动重复登录。
4. 按页读取所有成绩，限制页数和记录数。校验 ID、难度、成绩、comboStatus、syncStatus、DX 分数；拒绝重复谱面、循环分页、缺失字段及未知枚举。AP 只来自 comboStatus，不从达成率推断。
5. 仅对本次明确成功的登录尝试退出；读取结束或成绩失败都会执行此步骤。退出请求后再读 preview，明确不在登录状态时才标记 `logout_confirmed=true`。请求失败、状态仍在登录或未知时分别报告失败/未确认，绝不凭 HTTP 200 宣称退出成功。
6. 工作进程输出的每条成绩由主进程再校验。已取得完整成绩与退出会话确认是两个独立结果。失败/超时不应自动重试。

固定上游仍依赖非公开 DLL 行为；其二维码解析阶段有无额外服务端作用无法从 Python 层完全确认。此功能是会创建/退出游戏会话的主动同步，不是纯只读 NET 抓取。并发游戏状态可能变化，首次账号验证需确认实际行为。

## 进程边界

bubblewrap 使用独立 PID/user/mount 等命名空间，共享网络。只挂载只读系统库、证书、`/etc/alternatives`（Wine 启动器依赖）、运行库、后端源码，以及专用可写状态目录；应用数据库、应用密钥、用户 HOME 都不挂载。工作进程清空继承环境。只允许一个同步任务；180 秒超时和过大输出会终止进程组，PID 命名空间负责清理 Wine 后代。CPU/core dump/单文件大小有限制；Wine 需要很大的虚拟地址空间，不能以很小的 RLIMIT_AS 限制其内存。物理内存上限由独立服务的 cgroup 设置。broker 本身使用与应用相同的 UID；文件隔离边界是它启动的 bubblewrap 子进程，不应将整个 broker 误称为独立权限账号。

网络由 broker 的网络命名空间决定，bubblewrap 不是无网络沙箱。云服务器部署使用 [独立代理出口](../docs/WAHLAP_PROXY.md)，让 Python 和 Wine 共用 TUN；仅给 HTTPX 设置代理无法覆盖 DLL 的直接连接。平台的 namespace/AppArmor 限制必须实测，不允许失败后直接在主进程内运行。`available()` 检查文件和无凭据的 Python 沙箱探测，探测结果缓存一分钟；不代表账号链路已验证。手动 `probe_network()` 不使用缓存，通过同一沙箱请求游戏配置，不提交二维码或创建游戏会话。运行时或网络失败只返回固定错误分类及允许的阶段、HTTP 状态，不返回原始上游异常。

## 当前验证边界

离线测试验证了字段、分页、登录保护、退出确认逻辑、进程超时、响应上限及凭据隔离。2026-10-13 在目标服务器 Ubuntu 26.04.1 / Wine 10 / bubblewrap 0.11.1、实际应用用户及独立服务限制下，Python imports 和 Wine DLL exports 探测均通过，均未访问账号；首次 prefix 初始化内存峰值约 808 MiB，已初始化的完整探测约 113 MiB。

同日账号同步尝试未取得成绩。后续对照测试中，同一服务器、协议和配置请求，直连收到 HTTP 200 空响应，临时代理收到 384 字节响应并成功解析游戏配置。此结果支持出口受限的判断，但不能推断华立内部使用单 IP、网段还是 ASN 规则。长期代理必须另行验证；配置探测成功不等于二维码授权、成绩读取及退出会话已经全部成功。
