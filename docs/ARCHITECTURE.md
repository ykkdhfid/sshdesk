# 架构与实现说明

这份文档记录设计取舍和踩过的坑，方便自己回顾，也方便面试时讲清楚「为什么这么写」。

---

## 1. 分层

```
                    ┌─────────────┐      ┌─────────────┐
                    │   ui/ (Tk)  │      │   cli.py    │
                    └──────┬──────┘      └──────┬──────┘
                           │                    │
                    ┌──────┴────────────────────┴──────┐
                    │            core/                 │
                    │  client · terminal_screen · sftp │
                    │  monitor · diagnostics · executor│
                    │  config · credentials · audit    │
                    └──────────────┬───────────────────┘
                                   │
                              paramiko / socket
```

原则：**core 不 import tkinter**。这样：

- 同一套逻辑既能给 GUI 用，也能给 CLI 用（`exec` / `monitor` / `sftp` 全部可无界面运行）；
- 单元测试不需要显示器，`pytest` 能在服务器/CI 上跑；
- 换 UI 框架（比如以后换 PySide）不用动业务代码。

---

## 2. 终端渲染：这个项目真正的技术核心

### 2.1 为什么不能直接往 Text 里塞

`invoke_shell()` 返回的是原始字节流，里面混着控制序列：

```
\x1b[32m●\x1b[0m ssh.service - OpenBSD Secure Shell server\r\n
\x1b[2J\x1b[H                       # 清屏 + 光标归位
\x1b[1;31mError\x1b[0m                # 加粗红色
```

如果直接 `text.insert`，用户看到的是满屏 `^[[32m`。所以中间必须有一层**终端模拟器**：

```
bytes ──► 增量 UTF-8 解码 ──► 状态机（GROUND/ESC/CSI/OSC）──► 字符网格 ──► tkinter Text
```

`core/terminal_screen.py` 就是这一层，实现了一个够用的 VT100/xterm 子集：

| 类别 | 支持 |
|---|---|
| 光标 | CUP/CUU/CUD/CUF/CUB/CHA/VPA、保存与恢复（ESC 7/8、CSI s/u） |
| 擦除 | ED(0/1/2/3)、EL(0/1/2)、ECH、DCH、ICH |
| 行与屏 | IL/DL、滚动区 DECSTBM、SU/SD、RI/IND/NEL |
| 样式 | SGR 0/1/2/4/7、22/24/27、30-37/39、40-47/49、90-97/100-107、256 色 `38;5;n`、真彩 `38;2;r;g;b` |
| 其它 | OSC 标题、BEL、BS、TAB、CR/LF、`?25` 光标显隐 |

### 2.2 字符网格与「延迟换行」

网格是 `rows × cols` 的 `(字符, 样式)` 二维数组。几个容易写错的点：

- **LF 不回车**：`\n` 只下移一行，列不动；回车是 `\r`。很多实现把两者合并，会导致 `top` 输出错位。
- **延迟换行**：写满最后一列时光标停在该列，置 `pending_wrap=True`，下一个字符才真正换行。
  否则 80 列的行会多出一个空行，长命令输出会整屏乱跳。
- **擦除是填空格而不是删字符**：`EL`/`ED` 必须用当前背景色填空格，否则带背景色的进度条会被擦出黑洞。
- **滚动区**：`top`/`vim` 只滚动可视区，`_scroll_up` 必须只操作 `scroll_top..scroll_bottom`，
  并且在 `scroll_top == 0` 时才把行送进 scrollback。

### 2.3 中文双宽：终端按列，tkinter 按字符

这是最隐蔽的一个坑。

终端里「列」是显示宽度：一个汉字占 2 列、一个 ASCII 占 1 列。
但 `tk.Text` 的索引 `"3.5"` 里的 5 是**第 5 个字符**，不是第 5 列。一行里只要出现中文，
两者就不再相等，光标会跑到错误的位置，点击也会定位到别的字。

解法：渲染每一行时同时建立「列 → 字符索引」映射。

```python
col = 0
char_index = 0
for ch, attr in cells:
    mapping[col] = char_index    # 这一列对应行内第几个字符
    if ch == "":                 # 宽字符第二格的占位，不占字符
        col += 1
        continue
    width = char_width(ch)       # unicodedata.east_asian_width
    col += max(1, width)
    char_index += 1
```

光标定位时 `f"{row+1}.{mapping[cursor_c]}"`，中文行才对齐。

`char_width` 还处理了组合字符（占 0 列），否则 emoji / 重音字符会把整行推歪。

### 2.4 增量解码

一个汉字的 UTF-8 是 3 字节，`recv(4096)` 很可能把一个字劈成两半。
用 `codecs.getincrementaldecoder("utf-8")("replace")` 持有跨包状态，
否则偶发的「半个汉字」会变成乱码方块。测试里 `test_incremental_utf8_decode` 专门钉这一条。

### 2.5 性能

每 40ms 检查一次 `buffer.dirty`；渲染前算一个轻量指纹（所有非空格字符 + 光标位置），
指纹没变就跳过重绘。对 100×30 的屏幕，全量重绘约 3000 字符，在 Tk 里是可接受的。

---

## 3. 线程模型

Tk 不是线程安全的：**只有主线程能碰控件**。而 SSH 读取会长时间阻塞。

```
reader thread ──recv()──► queue.Queue ──► after(40ms) ──► ScreenBuffer.feed() ──► Text
      ▲                                                          (主线程)
      └── channel.send(bytes) ◄── 键盘事件 ─────────────────────────┘
```

- 读线程只做 `recv` 和入队，不碰任何控件；
- 主线程用 `after` 轮询队列，合并多次数据后一次性 `feed`，避免高频重绘；
- 写方向直接 `channel.send`，paramiko 的 send 是线程安全的。

### 一个跨线程的例外：主机指纹确认

`paramiko` 在握手阶段回调 `missing_host_key`，这个回调发生在**工作线程**里，
但确认框必须由主线程弹出。做法是「切线程 + 事件同步」：

```python
def _ask_host_key_threadsafe(self, hostname, key_type, fingerprint):
    event = threading.Event()
    answer = {"ok": False}
    def ask():                        # 这段会被 after 调到主线程执行
        answer["ok"] = messagebox.askyesno(...)
        event.set()
    self.after(0, ask)
    if not event.wait(timeout=120):
        return False                  # 超时按拒绝处理，安全优先
    return answer["ok"]
```

注意 `event.wait` 会卡住工作线程，这是有意的：握手必须等确认结果才能继续。

---

## 4. 连接层

### 4.1 主机密钥：TOFU

不做 `AutoAddPolicy`（自动信任一切）。流程是：

1. 加载 `~/.ssh/known_hosts` 和 `~/.sshdesk/known_hosts`；
2. 未见过的主机 → 回调让用户看指纹（`SHA256:...`）再决定；
3. 信任后写入本地 `known_hosts`（0600）；
4. 指纹变化 → `BadHostKeyException`，直接拒绝并提示「是否重装过服务器」。

指纹算法用标准 SSH 的 `SHA256:base64(sha256(key.asbytes()))`，和 `ssh-keygen -lf` 输出一致，
方便和服务器上的 `/etc/ssh/ssh_host_*_key.pub` 对账。

### 4.2 exec 的死锁陷阱

```python
stdin, stdout, stderr = client.exec_command(cmd)
out = stdout.read()                     # 必须先把输出读干净
err = stderr.read()
code = stdout.channel.recv_exit_status()
```

如果先 `recv_exit_status` 再读输出，命令输出超过通道窗口（默认 2MB 左右）时，
远端写满缓冲区阻塞、本地等退出码阻塞 —— 双向死锁。这是 paramiko 最常见的坑。

### 4.3 跳板机

不需要额外的 ssh 客户端，用 `direct-tcpip`：

```python
jump_transport = jump_client.get_transport()
sock = jump_transport.open_channel("direct-tcpip", (target_host, target_port), ("127.0.0.1", 0))
client.connect(hostname=target_host, sock=sock, ...)
```

`open_channel` 返回的是一个类 socket 对象，paramiko 的 `connect(sock=...)` 直接吃。

### 4.4 保活

`transport.set_keepalive(30)`。家用宽带/NAT 的空闲连接常被 5~10 分钟掐断，
不发保活包的话，回来一看终端已经「卡死」了。

---

## 5. 凭据加密

```
machine-id + 用户名 ──PBKDF2-HMAC-SHA256(200k)──► 32B key ──base64──► Fernet
                                                                      │
                                         credentials.enc ◄── encrypt({ref: password})
```

配置里只存 `password_ref`，永远不存明文。换机器后 `machine-id` 变了，密钥解不出来，
此时静默降级为空库（`InvalidToken` → `{}`），让用户重新输入，而不是崩掉。

**边界要讲清楚**：这挡的是「配置文件被顺手拷走/同步到网盘」，不是「本机被攻破」。
真要更强，应该接系统 keyring（macOS Keychain / Windows DPAPI / Linux Secret Service）。

---

## 6. 诊断为什么先探端口再读 banner

只 `connect()` 成功不够：可能连上的是被别的服务占用的 22 端口，或者中间有端口转发。
所以探测时额外读 64 字节，看是不是以 `SSH-2.0-` 开头：

```python
banner = sock.recv(64)
if banner.startswith(b"SSH-"):
    ok
else:
    "端口开放但未返回 SSH 欢迎语"
```

再结合 `is_private_host()`：`10/8`、`172.16/12`、`192.168/16`、`127/8`、`100.64/10` 都算不可直连。
一旦命中私网地址，结论里就直接给出「要么分配公网 IP，要么走跳板机」——
这正是阿里云学生机的典型场景，避免用户反复怀疑密码或 key。

诊断不登录，所以在「根本没开公网」的情况下也能跑出有意义的结论。

---

## 7. 批量执行

```python
with ThreadPoolExecutor(max_workers=6) as pool:
    futures = {pool.submit(run_on_server, s, cmd): s for s in targets}
    for future in as_completed(futures):
        ...
```

- 每台机器独立建连，一台超时不影响其他台；
- 用 `as_completed` 而不是按顺序 `result()`，慢机器不会拖住整体进度；
- 结果按输入顺序重排，方便对照；
- 单台异常在 `except Exception` 兜住，防止一个未预期错误炸掉整批。

---

## 8. 想继续加功能怎么改

| 想加的东西 | 改哪里 |
|---|---|
| 新的 CLI 子命令 | `cli.py` 加 `add_parser` + `cmd_xxx`，复用 core |
| 新的监控指标 | `monitor.COLLECT_COMMAND` 加一节 `#TAG`，`parse_snapshot` 里解析 |
| 终端支持鼠标上报 | `terminal_screen._csi` 加 `?1000h` 等模式位，`terminal_view._on_key` 发对应序列（滚轮回看已实现） |
| 换成 PySide 界面 | 只重写 `ui/`，`core/` 完全不动 |
| SSH 配置导入的更多指令 | `core/sshconfig.py` 的 `_apply()` 加 `LocalForward` / `UserKnownHostsFile` 等 |

---

## 9. 已知取舍

- **不做终端会话持久化**：断了就是断了，需要重连。tmux 是更合适的解法，客户端不该重复实现。
- **监控每次新建连接**：换来的是不会和终端/SFTP 抢同一个 transport；2C2G 机器上握手开销可以接受。
- **滚动历史只做回看**：不做 tmux 那样的复制模式搜索/选择；Web 端直接交给 xterm.js。
- **Web 版不做多用户**：单令牌单用户模型，权限体系留给反代或后续版本。
- **不做 SFTP 并发**：paramiko 的 SFTPClient 单通道，并发上传反而更慢；用队列串行更稳。

---

## 10. Web 版：同一套 core，换一个前端

### 10.1 为什么是 WebSocket

HTTP 轮询（每 100ms 拉一次输出）在交互式终端里延迟和请求数都不划算；SSE 只能单向。
WebSocket 是双向的二进制通道，正好承载「远端字节流进、按键字节流出」这两条对等的流。

服务端只做一件事：**把 paramiko 的阻塞 channel 翻译成 asyncio 的双向管道**。

### 10.2 阻塞 IO 怎么进事件循环

这里有两个不同频次的阻塞源，处理方式必须区分：

| 动作 | 频次 | 做法 |
|---|---|---|
| 握手、`open_shell`、`resize_pty`、`send` | 一次或按次 | `asyncio.to_thread(...)`，用完即还 |
| 读远端输出 | 每秒几十到几百次 | **专用线程 + `loop.call_soon_threadsafe(queue.put_nowait)`** |

如果读循环也用 `to_thread`，每 8KB 就要占用一次线程池调度，窗口里跑 `top` 时线程池会被打满。
所以读循环是「一个常驻线程 + 无界 `asyncio.Queue`」，只在跨线程投递时用到事件循环。

随后用两个任务并行：

```python
tasks = [send_output(), receive_input()]
done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
for task in pending:
    task.cancel()
```

任一方向结束（远端关闭 / 浏览器关页面）就取消另一个，再统一 `channel.close()` + `conn.close()`，
保证不会出现「浏览器关了但 SSH 还挂着」的泄漏。

### 10.3 鉴权：一个令牌，三处把关

- **HTTP**：`POST /api/login` 比对令牌，成功才下发 `HttpOnly` + `SameSite=Strict` 的 Cookie；
- **接口依赖**：`require_token` 作为 FastAPI 依赖挂在每个数据接口上；
- **WebSocket**：握手时读 Cookie，比对失败直接 `close(4401)`。

比对比的是 `hmac.compare_digest`，不是 `==`，避免长度/内容泄漏的时序差异。
登录失败按来源 IP 累计，60 秒内 5 次就 429，防止令牌被暴力枚举。

**一个必须踩过的坑**：ASGI 规范下在 `ws.accept()` 之前 `close()` 会被降级成 HTTP 403，
浏览器只能看到「连接失败」，拿不到 4401。所以顺序改成「先 accept，再校验，再按码关闭」。

### 10.4 终端尺寸：一个真实发生过的错行

服务端 `open_shell()` 时用的是默认 `100x30`。移动端屏幕窄，xterm 的 `fit()` 算出来可能只有
53 列。如果前端只在 `onResize` 里发尺寸，而首次 `fit()` 发生在 WebSocket 还没打开的时候，
这条消息就丢了 —— 远端继续按 100 列排版，bash 的 readline 在 53 列的窗口里重绘，整屏错行。

修法是**连接建立时无条件补发一次当前尺寸**（`onopen` 和收到 `ready` 后各发一次），
不依赖「尺寸是否变化」这个事件。验证方式很直接：在终端里跑 `stty size`，它必须等于
xterm 实际的行列数（截图里是 `31 53`）。

### 10.5 浏览器里没法弹窗确认指纹

桌面端首次连接会弹窗让你核对指纹，Web 端没有这个交互位。策略是**默认拒绝 + 复用**：

1. 未知主机 → 返回 4502 并提示「先在电脑上 `--trust-host` 确认一次」；
2. 电脑上确认后写进同一份 `~/.sshdesk/known_hosts`，Web 端直接通过；
3. 确实需要自动信任时才设 `SSHDESK_WEB_AUTO_TRUST=1`，且每次写审计 `web_auto_trust_hostkey`。

宁可让用户多一步，也不让浏览器悄悄接受一个没见过的密钥。

### 10.6 移动端的三个细节

- `100dvh` + 监听 `visualViewport`：软键盘弹出时可视高度变小，用 JS 回写 `--vh`，否则终端底部会被键盘挡住；
- `env(safe-area-inset-*)`：刘海屏/底部小白条不压住工具栏；
- xterm 的 `FitAddon` 在 `resize` / `orientationchange` 后重新 fit，并防抖 120ms，避免横竖屏切换时反复重排。

---

## 11. 滚动历史与 SSH 配置导入

### 11.1 回看：把两块缓冲拼成一条连续的行

`ScreenBuffer` 只在 `scroll_top == 0` 时把顶行推进 `scrollback`（滚动区域内的滚动不该进历史），
所以历史是「已经滚出屏幕的行」，grid 是「当前这一屏」，两块拼起来才是完整的行序列。

可见窗口就是在这条序列上开一个 `rows` 高的窗口，`offset` 表示窗口底部离序列末尾有多远：

```python
index = len(scrollback) - offset + row     # offset=0 时正好落在 grid 上
```

第一版把 `start` 写成了 `len(scrollback) + rows - offset`，offset 稍大就会越过 `grid` 上界，
渲染时取到 `None` 属性直接崩。这类偏移公式必须用「offset=0 应精确落回 grid[0]」来反推验证。

渲染复用同一套 `_render_row(cells)`：回看模式下只是换了单元格来源，样式、宽字符映射、
列→字符索引映射全部不变；光标只在 `offset == 0` 时绘制。按下任意键自动回到最新，
和真实终端的习惯一致。

### 11.2 ssh config：只读，且不做猜测式展开

解析器只认 `Host` / `HostName` / `User` / `Port` / `IdentityFile` / `ProxyJump`，并且守住三条边界：

- **只有 `Host *` 才是全局默认**。`Host *.example.com` 这种带域名的通配块语义依赖匹配，
  静态导入时整块忽略，而不是拿它当默认值污染其他主机（这个 bug 真实发生过）；
- **多别名取第一个**：`Host staging web2` 只导入 `staging`；
- **私钥不存在就退回 agent**：`IdentityFile` 指向的文件在本机不存在时，强行按 key 认证会在
  `validate()` 里直接报错，导入完一堆不可用的配置。不如如实降级成 agent，并把 `key_path` 置空。

`Host=foo` 这种等号写法、`%d`（本地 home）占位、行内注释都做了兼容。
导入只写 SSHDesk 自己的 `servers.json`，**绝不修改用户的 `~/.ssh/config`**。

---
