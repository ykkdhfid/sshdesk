# 推到 GitHub：三步

这个目录已经 `git init` 并完成了首次提交，只差一个远程仓库。

## 1. 在网页上建空仓库

打开 https://github.com/new

- Repository name：`sshdesk`
- 可见性随意（作品集建议 Public）
- **不要**勾选 "Add a README file"、"Add .gitignore"、"Choose a license"——本地都有，勾了会冲突

建完会得到一个地址，形如 `https://github.com/<你的用户名>/sshdesk.git`。

## 2. 本地绑定远程并推送

在本目录执行（把地址换成你自己的）：

```bash
git remote add origin https://github.com/<你的用户名>/sshdesk.git
git branch -M main
git push -u origin main
```

如果之前绑定过别的地址，先改：

```bash
git remote set-url origin https://github.com/<你的用户名>/sshdesk.git
```

## 3. 如果 push 要求账号密码

HTTPS 方式不能用密码，需要 Personal Access Token：

1. https://github.com/settings/tokens → Generate new token (classic)
2. 勾选 `repo` 权限，有效期自定
3. 推送时用户名填你的 GitHub 用户名，密码处粘贴 token

或者用 SSH：

```bash
ssh-keygen -t ed25519 -C "your@email.com"      # 已有可跳过
cat ~/.ssh/id_ed25519.pub                       # 贴到 https://github.com/settings/keys
git remote set-url origin git@github.com:<你的用户名>/sshdesk.git
git push -u origin main
```

---

## 推完检查三件事

1. 仓库首页能看到 README 里的徽章和动图入口；
2. `docs/DEMO.md` 点进去，三张 GIF 能正常播放；
3. 没有把 `.sshdesk/`、`web_token`、`servers.json` 之类的本地数据传上去
   （`.gitignore` 已经挡住，`git status` 里也不该出现）。

第 3 点尤其重要：服务器地址、用户名、令牌一旦进了公开仓库，
GitHub 的爬虫几分钟内就能扫到。
