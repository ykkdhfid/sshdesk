# 开发与排障日志

这个项目从 Linux 容器一路搬到 Windows 真机上双击可用，中间记了一堆坑，按遇到的顺序写在这里。
没什么体系，就是当时怎么想、查了什么、哪一步想错了、最后改了什么。

---

## 一、容器里联调，被逼出三个 bug

靶机是 `tools/mock_sshd.py`（2222 端口），跟 Web 服务一起在 Linux 容器里跑。
顺手写了两个检查脚本：接口 20 项，浏览器流程 26 项，手机 390 和桌面 1440 两档视口。
跑下来全绿，但绿的过程中冒出来三个问题，都不是脚本本身的错。

### 1. 我自己写的提示，少了一个参数

`web/server.py` 的 `_auto_trust_handler`，主机指纹未知时会告诉用户执行这个：

```
sshdesk test <机器名> --trust-host
```

可 `cmd_test` 只有带上 `--login` 才真的做认证握手、才把指纹写进 known_hosts。
所以照着提示敲的人，永远信任不上，然后每次连接都被拦一次。

提示是我写的，等于自己给自己挖坑。改成 `test <机器名> --login --trust-host`。

### 2. 密码只有一条路径认，另一条装看不见

`cli.py` 的 `_connect()` 会读 `SSHDESK_PASSWORD` 环境变量，但 `core/client.py` 的
`_resolve_password()` 根本不查这个变量，只翻凭据库。

结果 Web 端和 `run_batch` 拿到的密码永远是 `None`，报出来的是「无法连接到服务器」。
一句「无法连接」，能让人往网络、防火墙、端口方向查一整天，实际只是取密码的路少了一条。

改法是 `_resolve_password()` 先查凭据库，查不到再读环境变量；`connect()` 开头补一个明确的
`SSHError("没有可用的登录密码")`。宁可报得直白点，也别让人误判方向。

### 3. 靶机脚本里两个低级错

- `paramiko.AUTH_OK` 这个常量不存在，真名是 `AUTH_SUCCESSFUL`
- 少了 `check_channel_exec_request` 回调。监控采集走的是 exec 不是 shell，没有这个回调，channel 会被直接关掉

补了 `subprocess.run` 加 `send` / `send_stderr` / `send_exit_status` / `close` 就通了。

顺带还纠正了检查脚本自己的一个误判：WebSocket 终端的输出走**二进制帧**（`ws.send_bytes`），
只有 `ready` / `error` / `closed` 是 JSON 文本。原脚本把所有消息都当 JSON 解析，
于是那一批「没有回显」全是假失败。

---

## 二、搬到 Windows，先被环境绊了两跤

### 4. 虚拟环境建在带中文的路径里

项目放在 `E:\ssh Linux远程连接\`，目录名有中文还有空格。`python -m venv` 本身建得起来，
麻烦在后面：这个路径下每次 pip 干活都得小心编码。

### 5. `pip install -U pip` 中断了一次，pip 自己坏了

升级到一半被打断，`.venv` 里的 pip 只剩半套：

```
No module named 'pip._vendor.packaging'
```

pip 起不来，也就没法再用 pip 去修 pip。当场有点懵。

最后是 `python -m ensurepip --upgrade` 重建，或者干脆删掉 `.venv` 重来。这次两个都做了：
删掉重建，再 ensurepip 补一遍。

### 6. 有两个测试在 Windows 上必然挂（但产品没坏）

`pytest` 跑 88 个，挂 2 个。看断言就知道是测试自己写了平台假设：

- `test_expand_identity_path` 断言结果以 `/` 开头、以 `/.ssh/id_rsa` 结尾 —— Windows 的 `Path` 用反斜杠
- `test_token_file_permissions` 断言权限 `0600` —— Windows 不映射 POSIX chmod

改成用 `Path` 对象比较；权限那条加 `if sys.platform != "win32"` 才检查。

### 7. 顺着这个测试，发现产品代码也有一处

同一个 `expand_identity_path`，在 Windows 上返回的是：

```
C:\Users\x/.ssh/id_rsa
```

正反斜杠混在一起。`open()` 能吃，但打印出来难看，而且说明「假设路径一定用 /」这个毛病不只测试里有。

`return str(Path(os.path.expanduser(value)))`，交给 `Path` 统一就好。

改完 88 个测试全过，GUI 也能起来。

---

## 三、启动器，绕了一大圈

这一段纯属自找。教训一句话：别在 GBK 的 cmd 里跟中文路径较劲。

### 8. cmd 把中文路径吃成了乱码

用户发来的报错是这一串：

```
'✤巯■纸Windows锜?cd' 不是内部或外部命令
```

整串被当成命令名了。不是命令写错，是粘贴进来的中文路径在 GBK 代码页下变成了乱码字节。
所以只要路径里有中文，在 cmd 里手敲命令这条路基本没救。要手敲就换 PowerShell。

### 9. bat 文件存 GBK，却在里面 chcp 到 UTF-8

第一版 `启动桌面版.bat` 我加了 `chcp 65001`。问题是脚本本身存的是 **GBK**，
路径里又有「源码」两个字。

代码页一换，脚本里的「源码」就成了乱码字节，`cd` 找不到目录，程序退到上级目录，
在那儿建了个空壳 `.venv`，然后：

```
The system cannot find the path specified.
No module named sshdesk
```

同一个 bat，前后两行原因完全不同，看报错还以为是包没装好。

改法两条：去掉 `chcp`（保持 GBK，和文件编码一致），并且不再靠 `cd` 定位：

```bat
set "ROOT=%~dp0源码\sshdesk"
set "PYTHONPATH=%ROOT%"
"%ROOT%\.venv\Scripts\python.exe" -m sshdesk gui
```

顺手把上级目录那个空壳 `.venv` 删了。双击出窗口。

### 10. `-m sshdesk` 是认当前目录的

包没有 `pip install` 进环境，`-m sshdesk` 靠 cwd 导入。换个目录执行同一个 `python.exe`，
必然 `No module named sshdesk`。

这条写进 `安装说明.md` 了：手敲命令先 `cd` 到源码目录。

---

## 四、终端打不进字，底下压着两个问题

表象只有一个：连上了，看得到提示符，敲字母没反应。
但底下其实是两件不相干的事，得先剥开一层才看得到第二层。

### 11. 输入法把字母接去组词了

先用 WinAPI 往运行中的窗口发按键（`SetForegroundWindow` 加 `SendKeys`），看它到底收没收到：

- 发 `echo KEYTEST` 加回车：服务器收到非法字节，`command not found`
- 发 `abc`：只进去一个大写 `A`
- 先按 Shift 再发 `abc`：`abc` 出来了，同时**弹出输入法候选框**（`1 按不出 2 阿百川 3 ABC…`）

候选框一出来就没什么可争的了：字母正在被 IME 组词，上屏之前一个字符都到不了终端。
而回车不被输入法接管，所以「只有回车能用」这个现象本身就在指向它。

### 12. 真正的原因是 Alt 状态位，每个字符前面都多一个 ESC

输入法那条只能解释「打不出字」，解释不了另一张截图里的东西：

```
root@host:~# ^[b^n^m^]^[^ , ^[^.^[^[^[^[^[^]a^[b^[c
```

`^[` 是 ESC 的可见形态。每个字母前面都有一个，说明程序在给每个字符前塞 `\x1b`。去看代码：

```python
alt = bool(state & 0x8)
...
if event.char and event.char.isprintable():
    self.send(("\x1b" + event.char) if alt else event.char)
```

为了确认，写了个只打印 `keysym / char / state` 的最小 Tk 窗口，用 SendKeys 向它发 a、b、c，
手不按任何修饰键：

```
keysym='a' char='a' state=0x8
keysym='b' char='b' state=0x8
keysym='c' char='c' state=0x8
```

**Windows 上的 Tk，就算不按修饰键，`event.state` 也恒为 `0x8`。**
X11 里 `0x8` 是 Mod1（Alt），Windows 上 Alt 走的是 `0x20000`。

所以程序一直认为「Alt 按着」，给每个字符前面加了个 ESC，服务器收到的是 `Alt+a`、`Alt+b`，
bash 的 readline 当元命令吃掉了，屏幕上就只剩一串 `^[`。

```python
if sys.platform == "win32":
    alt = bool(state & 0x20000)
else:
    alt = bool(state & 0x8)
```

在真实连着的窗口里敲了一遍：

```
root@host:~# echo abcdefg
abcdefg
```

### 13. 顺手把输入法整层摘掉

状态位修好之后，中文输入法还是会把字母截走。Tk 的 Text 没法从应用层绕过 IME，
但在 Win32 层可以把控件的输入法上下文解绑：

```python
imm32.ImmAssociateContext(hwnd, 0)
```

实测量过：摘之前 `ImmGetContext` 返回一个有效句柄，摘之后返回 NULL。

代价是终端里不能直接打中文了。要输中文就在别处打好再粘进来。
SSH 命令行基本用不到中文，我觉得这个交换划算，就留下了。

### 14. 光标跑到窗口最下面，还复制粘贴失灵

这是后面补的两件事，起因是用户说「光标在下面」「看不到光标」和「不能复制粘贴」。

光标这一点我一开始判断错了，说是 tkinter 的 `Text` 自带插入符在作怪——那个跟着鼠标跑的竖线确实是另一个东西，
关掉它（`insertwidth=0`）之后屏幕上干净了，但**真正的问题没解决**：终端光标还是不显示。

翻渲染代码才看出来，`_render` 里是这么写的：

```python
for row in range(self.buffer.rows):
    line_chars, mapping = self._render_row(...)
    if self._view_offset == 0:
        cursor_index = (row, mapping.get(self.buffer.cursor_c, line_chars))
```

`cursor_index` 在每次循环里都被覆盖，循环结束它就等于**最后一行**。于是不管光标在哪，
反色方块都画在窗口最底下那行——用户说的「光标跑到窗口最下面」其实就是它，跟插入符没关系。

改掉之后还剩一个问题：光标落在行尾时，后面没有字符可以染色，`tag_add` 加了个空范围，
等于什么都没画。所以在行尾补一个空格当占位：

```python
if self._view_offset == 0 and row == self.buffer.cursor_r:
    cursor_index = (row, mapping.get(self.buffer.cursor_c, line_chars))
...
if c >= line_len:
    self.text.insert(f"{r + 1}.end", " ")
```

`insertwidth=0` 那条也留着，不然 Text 的竖线会跟着鼠标在终端里到处跑，看着更乱。

改完这两处，按说该好了。结果用户说还是看不到，又得接着查。

第三层是 tag 优先级。`_render_row` 给**每个字符**（包括空格）都挂了 ANSI 的 tag，
`_tag_for` 里连 `background` 都写了——毕竟反显要靠背景色。而 Tk 里后创建的 tag 优先级更高，
`cursor` 是在 `_build_ui` 里早早建好的，于是字符自己的黑底把光标的反色背景盖掉了，等于没画。

再回头一看，之前光标之所以"看得见"，恰恰是因为它一直画在窗口最后一行——那行是空的，
没有字符也就没有 ANSI tag，反色才露得出来。把一个 bug 修掉，光标挪到有字符的行上，就彻底消失了。
一个错误一直在替另一个错误打掩护。

补一行把光标提到所有 tag 上面，这才真的显示出来：

```python
self.text.tag_add("cursor", idx, f"{r + 1}.{c + 1}")
self.text.tag_raise("cursor")
```

复制粘贴那个简单——`Ctrl+Insert` / `Shift+Insert` 压根没绑，按键落进 `<Key>` 分支被当成终端输入，
`Insert` 的转义序列 `\x1b[2~` 直接发给了服务器，bash 原样插进去还回显出来。
用户截图最后一行那串 `^[[2~` 就是这个，不是「复制没反应」。

补上 `Ctrl+Insert` 复制、`Shift+Insert` 粘贴、`Ctrl+V` 粘贴；
`Ctrl+C` 有选区时复制、没选区才发 `\x03` 中断（跟 Windows Terminal 一致）；`Ctrl+Shift+C/V` 保留；
另外加了个右键菜单。剪贴板放 `echo from-clipboard`，`Shift+Insert` 之后命令完整出现在提示符后面。

---

## 五、界面：糊、小、挤

### 15. 整个窗口被系统当位图拉伸，字是糊的

用户的原话是「分辨率太低了」。看了下，不是分辨率的事——程序没声明 DPI 感知，
Windows 在 125% / 150% 缩放下会先渲染一张位图再放大，等于把界面拍成照片再拉伸。

得在创建 Tk **之前**声明，顺序反了不生效：

```python
ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor v2
```

失败就逐级退回 `1`，再退 `SetProcessDPIAware()`。

### 16. 窗口尺寸写死 1200x780

1080p 上偏小，缩放屏上又顶到屏幕下边缘。改成按屏幕算：宽 90%、高 88%，居中。
这台机器上落地是 1737×997 物理像素，和写死的 1200×780 差挺多。

### 17. 窗口一矮，底部按钮就被挤出去了

文件页底部那排「下载选中 / 上级目录 / 新建目录 / 删除」只剩半截，用户发了截图过来。

原因是中间的文件列表用了 `pack(fill="both", expand=True)`，抢空间时优先保自己。
pack 是按顺序分配空间的，谁先 pack 谁先拿，底部那排最后 pack，空间不够就整个被裁掉。

把底部这排改成 `side="bottom"` 先占位，列表再去吃剩下的：

```python
actions.pack(side="bottom", fill="x", pady=(8, 0))
...
panes.pack(side="top", fill="both", expand=True)
```

验证的办法有点粗暴：把文件页塞进一个 620px 高的容器（比正常窗口矮得多），
四个按钮底边都落在 y=608，全在可视区里。

### 18. 本地那栏的「上级目录」点了没反应

`_go_up` 只改了远端目录：

```python
def _go_up(self):
    if self.browser:
        self.remote_dir = posixpath.dirname(...)
        self.load_remote()
```

本地目录一动没动，用户看着当然就是「按钮坏了」。改成两边各退一层，
并且用 `parent != self` 挡住盘符根上的死循环。

---

## 最后跑一遍

每次改完都重跑这几项，当前状态：

- `python -m pytest -q` → 88 passed
- 双击 `桌面版.bat` → 窗口正常，标题对
- 连 Ubuntu 云主机 → 提示符、banner、MOTD 都对
- 终端里敲 `echo abcdefg` → 完整回显并输出
- 光标 → 提示符后面一个反色方块，光标在哪行就画在哪行
- 文件页压到 620px 高 → 四个按钮都在
- 点「上级目录」→ 本地远端各退一层
- 接口联调 → 20/20（[E2E-CHECK.md](E2E-CHECK.md)）
- 浏览器流程 → 26/26（[E2E-CHECK.md](E2E-CHECK.md)）

---

## 回头看看

写完之后翻了一遍，发现这些坑的形状其实挺像的，主要是三种：

一是平台假设没清干净。路径分隔符、文件权限、`state` 位掩码，在 Linux 上都成立，到 Windows 就崩。
写测试的时候把 `/` 和 `0600` 写死在断言里，是最典型的两处。

二是报错信息会骗人。「无法连接」其实是缺密码，「命令找不到」其实是编码坏了路径，
「打不进字」其实是每个字符前面多塞了个 ESC。这三次我都是先信了第一直觉，白跑一段。

三是先量再改比什么都省时间。那个只打印 `state` 的探针写了不到五分钟，
省掉了在 `send()` 和编码上乱改的两个小时。前面几轮要是早点写探针，估计能少绕两圈。
