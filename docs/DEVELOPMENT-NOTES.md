# 开发与排障日志

从容器里的端到端联调，到 Windows 真机上双击可用，中间踩到的每一个坑都记在这里。
每条都写清楚：**现象 → 怎么定位 → 根因 → 怎么修 → 怎么验证**。
写它是为了让「下次再遇到」有据可查，也让评审者看到问题是怎么被一层层剥开的。

---

## 阶段一：端到端联调，抓到 3 个真 bug

在 Linux 容器里起了靶机（`tools/mock_sshd.py`，2222 端口）和 Web 服务，写了两个检查脚本：
接口 20 项、浏览器流程 26 项（手机 390 与桌面 1440 两档视口）。跑下来全过，但过程中逼出了三个缺陷。

### 1. 提示让用户执行的命令，少了一个关键参数

`web/server.py` 的 `_auto_trust_handler` 在主机指纹未知时提示：

```
sshdesk test <机器名> --trust-host
```

但 `cmd_test` 只有带上 `--login` 才会真正做认证握手、才会写 `known_hosts`。
照提示照做的用户永远信任不上指纹，然后每次连接都被拦。

**修法**：提示改成 `test <机器名> --login --trust-host`。

### 2. 密码只在一条路径上被读取

`cli.py` 的 `_connect()` 会读 `SSHDESK_PASSWORD` 环境变量，但 `core/client.py` 的
`_resolve_password()` 不认这个变量——它只查凭据库。

于是 Web 端和 `run_batch` 拿到的密码永远是 `None`，报的是「无法连接到服务器」，
而不是「缺密码」。**错误信息把人引向了网络方向，实际是凭据解析漏了一条路。**

**修法**：`_resolve_password()` 先查凭据库，查不到再读环境变量；
`connect()` 开头加一个明确的 `SSHError("没有可用的登录密码")`。

### 3. 靶机本身的两个错

- `paramiko.AUTH_OK` 不存在，常量名是 `AUTH_SUCCESSFUL`
- 缺 `check_channel_exec_request`：监控采集走的是 exec 而不是 shell，
  没有这个回调，channel 会被直接关掉

**修法**：补 `subprocess.run` + `send` / `send_stderr` / `send_exit_status` / `close`。

> 另外修正了检查脚本自身的误判：WS 终端输出走**二进制帧**（`ws.send_bytes`），
> 只有 `ready` / `error` / `closed` 是 JSON 文本。原脚本按 JSON 解析所有消息，
> 于是「没有回显」全是假失败。

---

## 阶段二：搬到 Windows 本机

### 4. 虚拟环境建在含中文的路径里

项目放在 `E:\ssh Linux远程连接\`，目录名带中文和空格。`python -m venv` 本身能建，
但后续 pip 在这个路径下操作时要小心编码。

### 5. `pip install -U pip` 被打断，pip 自己坏了

中途中断了一次升级，结果 `.venv` 里的 pip 只剩半套：

```
No module named 'pip._vendor.packaging'
```

pip 自己起不来，也就没法再用 pip 修 pip。

**修法**：`python -m ensurepip --upgrade` 重建 pip，或直接删掉 `.venv` 重来。
这次选了删掉重建 + `ensurepip`。

### 6. 两个测试在 Windows 上必然失败（不是产品缺陷）

跑 `pytest`，88 个里有 2 个挂：

| 测试 | 断言 | 为什么在 Windows 上必挂 |
|---|---|---|
| `test_expand_identity_path` | 结果以 `/` 开头、以 `/.ssh/id_rsa` 结尾 | Windows 的 `Path` 用反斜杠 |
| `test_token_file_permissions` | 文件权限是 `0600` | Windows 不映射 POSIX chmod |

**修法**：前者改成用 `Path` 对象比较（跨平台）；后者加 `if sys.platform != "win32"` 才检查。

### 7. 顺手修掉产品代码里一个真问题

同一处 `expand_identity_path` 在 Windows 上返回：

```
C:\Users\x/.ssh/id_rsa
```

正反斜杠混着。虽然 `open()` 能吃，但打印出来很难看，也是平台假设没清干净。

**修法**：`return str(Path(os.path.expanduser(value)))`，交给 `Path` 统一分隔符。

修完 88 个测试全过，GUI 能正常启动。

---

## 阶段三：启动器与 cmd 编码

这一段是两个自找的坑，写下来提醒自己：**别在 GBK 的 cmd 里跟中文路径较劲**。

### 8. cmd 的 GBK 代码页把中文路径吃成乱码

用户截图里的报错：

```
'✤巯■纸Windows锜?cd' 不是内部或外部命令
```

整串被当成命令名——不是命令写错，是粘贴的中文路径在 GBK 代码页下变成了乱码字节。

**结论**：要在 cmd 里敲含中文路径的命令，必然出问题。要手敲就用 PowerShell。

### 9. 我写的 bat 自相矛盾：文件存 GBK，却 chcp 到 UTF-8

第一版 `启动桌面版.bat` 里加了 `chcp 65001`。但脚本文件本身存成 **GBK**，
而路径里有「源码」两个字。

代码页一换，脚本里的「源码」就变成乱码字节 → `cd` 找不到目录 →
程序退到上级目录 → 在那里建了个空壳 `.venv` → 报：

```
The system cannot find the path specified.
No module named sshdesk
```

**修法**：去掉 `chcp`（保持 GBK，与文件编码一致），并且**不靠 `cd` 定位**：

```bat
set "ROOT=%~dp0源码\sshdesk"
set "PYTHONPATH=%ROOT%"
"%ROOT%\.venv\Scripts\python.exe" -m sshdesk gui
```

同时清掉上级目录那个空壳 `.venv`。实测双击能正常出窗口。

### 10. `-m sshdesk` 依赖当前目录

包没有 `pip install` 进环境，`-m sshdesk` 靠 cwd 导入。在别的目录执行同一个
`python.exe`，必然：

```
No module named sshdesk
```

这条写进了 `安装说明.md`：手敲命令务必先 `cd` 到源码目录。

---

## 阶段四：终端打不进字（两个独立故障叠在一起）

这一段的表象只有一个：「能连上、能看到提示符、但敲字母没反应」。
实际上底下压着**两个互不相干**的问题。排查时按「输入法 → 状态位」的顺序才剥干净。

### 11. 输入法在中文状态，把字母接走去组词

**现象**：连上服务器后敲字母毫无反应，只有回车生效，屏幕上堆出一串空提示符。

**定位**：用 WinAPI `SetForegroundWindow` + `SendKeys` 向运行中的窗口发按键：

| 发送内容 | 终端表现 |
|---|---|
| `echo KEYTEST` + 回车 | 服务器收到非法字节，报 `command not found` |
| `abc` | 只进去一个大写 `A` |
| 先按 Shift 再发 `abc` | `abc` 出现，同时**弹出输入法候选框** |

候选框（`1 按不出 2 阿百川 3 ABC…`）是决定性证据：字母正在被 IME 组词，
上屏之前一个字符都到不了终端。**回车不被输入法接管**，所以「只有回车能用」
这个症状本身就在指向输入法。

### 12. Alt 状态位误判：每个字符前面多了一个 ESC（真凶）

上面那条只解释了「打不出字」，解释不了另一张图里的东西：

```
root@host:~# ^[b^n^m^]^[^ , ^[^.^[^[^[^[^[^]a^[b^[c
```

`^[` 是 ESC 的可见形态。每个字母前面都有一个——说明程序给每个字符前塞了 `\x1b`。

看代码：

```python
alt = bool(state & 0x8)
...
if event.char and event.char.isprintable():
    self.send(("\x1b" + event.char) if alt else event.char)
```

**探针实测**（一个只打印 `keysym / char / state` 的最小 Tk 窗口，向它发 a、b、c，
手没按任何修饰键）：

```
keysym='a' char='a' state=0x8
keysym='b' char='b' state=0x8
keysym='c' char='c' state=0x8
```

**Windows 上的 Tk 即使不按修饰键，`event.state` 也恒为 `0x8`。**
X11 里 `0x8` 是 Mod1（Alt），但 Windows 上 Alt 实际用 `0x20000` 表示。

于是一直被判成「按着 Alt」→ 每个字符前加 ESC → 服务器收到 `Alt+a`、`Alt+b`，
bash 的 readline 把它当元命令吃掉 → 屏幕上只剩下一串 `^[`。

**修法**：

```python
if sys.platform == "win32":
    alt = bool(state & 0x20000)
else:
    alt = bool(state & 0x8)
```

**验证**（在真实连接的终端里敲的）：

```
root@host:~# echo abcdefg
abcdefg
```

完整输入、回车执行、正常输出。

### 13. 顺带把输入法整层摘掉

即使修了状态位，中文输入法仍会截走字母。Tk 的 Text 没法从应用层绕过 IME，
但可以在 Win32 层把该控件的输入法上下文解绑：

```python
imm32.ImmAssociateContext(hwnd, 0)
```

实测：摘除前上下文有效（返回一个非空句柄），摘除后 `ImmGetContext` 返回 NULL。

**代价**：终端里不能直接打中文。真要输就在别处打好再粘贴。SSH 命令行极少需要，这是有意的取舍。

---

## 阶段五：界面

### 14. 整个窗口被系统当位图拉伸，字是糊的

**现象**：界面看着「分辨率低」、字发虚，控件边缘不锐利。

**根因**：程序没声明 DPI 感知。Windows 在 125% / 150% 缩放下会渲染一张位图再放大，
等于把界面拍成照片再拉伸。

**修法**：创建 Tk **之前**声明（顺序错了无效）：

```python
ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor v2
```

失败逐级退回 `1`、`SetProcessDPIAware()`。

### 15. 窗口尺寸写死 1200x780

1080p 上偏小，缩放屏上又容易顶到屏幕下边缘。改成按屏幕算（宽 90%、高 88%，居中）。
这台机器上变成约 1737×997 物理像素。

### 16. 窗口一矮，底部按钮就被挤出可视区

**现象**：文件页底部「下载选中 / 上级目录 / 新建目录 / 删除」只剩半截。

**根因**：中间的文件列表用 `pack(fill="both", expand=True)`，抢空间时优先保自己；
pack 是按顺序分配空间的，**先 pack 的先拿**，底部那排最后 pack，空间不够就被裁。

**修法**：底部这排改成 `side="bottom"` **先占位**，列表再吃剩下的。

```python
actions.pack(side="bottom", fill="x", pady=(8, 0))
...
panes.pack(side="top", fill="both", expand=True)
```

**验证**：把文件页塞进 620px 高的容器（比正常窗口矮得多），四个按钮底边都在 y=608，全部可见。

### 17. 本地那栏的「上级目录」点了没反应

`_go_up` 只改了远端目录：

```python
def _go_up(self):
    if self.browser:
        self.remote_dir = posixpath.dirname(...)
        self.load_remote()
```

本地目录一动不动，用户看着就是「按钮坏了」。

**修法**：两边各退一层，并用 `parent != self` 防止在盘符根上死循环。

**验证**：`C:\Users\<用户名>\.android\studio` → `C:\Users\<用户名>\.android`

---

## 验证清单

每次改动后重跑，当前状态全绿：

| 项 | 方法 | 结果 |
|---|---|---|
| 单元测试 | `python -m pytest -q` | 88 passed |
| 桌面版启动 | 双击 `桌面版.bat` | 窗口正常，标题正确 |
| 真机连接 | 连一台 Ubuntu 云服务器 | 提示符、banner、MOTD 正常 |
| 终端输入 | 终端里敲 `echo abcdefg` | 完整回显并输出 `abcdefg` |
| 文件页底栏 | 窗口压到 620px 高 | 四个按钮全部可见 |
| 本地退层 | 点「上级目录」 | 本地与远端各退一层 |
| 接口联调 | 见 [E2E-CHECK.md](E2E-CHECK.md) | 20/20 |
| 浏览器流程 | 见 [E2E-CHECK.md](E2E-CHECK.md) | 26/26 |

---

## 复盘：这几类坑的共同形状

1. **平台假设没清干净**——路径分隔符、文件权限、`state` 位掩码，在 Linux 上都成立，到 Windows 就崩。
   测试断言里写死 `/` 和 `0600` 是最典型的两个。
2. **错误信息会骗人**——「无法连接」实际是缺密码；「命令找不到」实际是编码坏了路径；
   「打不进字」实际是每个字符前面多了个 ESC。**信现象，别信第一直觉。**
3. **先量再改**——探针打印 `state=0x8` 只花了两分钟，省掉了在 `send()` 和 UTF-8 编码上瞎改的两小时。
4. **中文路径 + GBK 是天生冤家**——不要试图在 cmd 里绕过，直接换 PowerShell 或把路径交给 `%~dp0`。
