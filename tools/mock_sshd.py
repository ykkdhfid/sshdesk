"""最小 SSH 靶机：给演示 GIF 用，支持密码认证 + PTY + 交互 shell。"""
import os
import pty
import select
import subprocess
import socket
import threading

import paramiko

PORT = 2222
PASSWORD = "test123"
HOST_KEY_PATH = "/tmp/mock_ssh_host_rsa"


class Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return (paramiko.AUTH_SUCCESSFUL if password == PASSWORD
                else paramiko.AUTH_FAILED)

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return (paramiko.OPEN_SUCCEEDED if kind == "session"
                else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED)

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth,
                                  pixelheight, modes):
        return True

    def check_channel_shell_request(self, channel):
        return True

    def check_channel_exec_request(self, channel, command):
        """监控采集走 exec 而不是 shell；没有这个钩子，channel 会被直接关掉。"""
        def run():
            p = subprocess.run(command, shell=True, capture_output=True, text=True)
            channel.send(p.stdout)
            channel.send_stderr(p.stderr)
            channel.send_exit_status(p.returncode)
            channel.close()

        threading.Thread(target=run, daemon=True).start()
        return True


def bridge(channel):
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["PS1"] = (r"\[\e[1;32m\]demo@sshdesk\[\e[0m\]:"
                             r"\[\e[1;34m\]~\$\[\e[0m\] ")
        os.execv("/bin/bash", ["/bin/bash", "--norc", "--noprofile", "-i"])

    try:
        while True:
            r, _, _ = select.select([channel, fd], [], [], 0.2)
            if channel in r:
                data = channel.recv(4096)
                if not data:
                    break
                os.write(fd, data)
            if fd in r:
                try:
                    out = os.read(fd, 4096)
                except OSError:
                    break
                if not out:
                    break
                channel.send(out)
    finally:
        channel.close()
        try:
            os.close(fd)
        except OSError:
            pass


def handle(client, addr):
    transport = paramiko.Transport(client)
    transport.add_server_key(paramiko.RSAKey(filename=HOST_KEY_PATH))
    transport.start_server(server=Server())
    chan = transport.accept(20)
    if chan is None:
        return
    bridge(chan)


def main():
    if not os.path.exists(HOST_KEY_PATH):
        paramiko.RSAKey.generate(2048).write_private_key_file(HOST_KEY_PATH)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", PORT))
    sock.listen(10)
    print(f"mock sshd on {PORT}", flush=True)
    while True:
        try:
            client, addr = sock.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(client, addr), daemon=True).start()


if __name__ == "__main__":
    main()
