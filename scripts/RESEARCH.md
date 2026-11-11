# 长期研究流程

所有研究会话使用 `optimize/research`。不要每周从 main 另开实验分支。
同一时刻只允许一个研究会话；远端分支 `stockai-research-lock` 是互斥锁（不是实验分支，只增不删：最新提交说明 LOCKED=占用、RELEASED=空闲）。
用分支而不是 tag，是因为云端会话的 git 代理禁止推送 tag、删除和强推远端引用；取锁/释放都只做普通的快进推送。
所有会话（手动、试运行和定时任务）都必须遵守；旧会话绕过脚本仍可能制造冲突。

## 每周操作

1. 将定时任务的初始分支设置为 `optimize/research`（新容器用 `git clone --branch optimize/research <仓库地址>`）。已有干净工作区先 `git fetch origin`、`git switch optimize/research`、`git merge --ff-only origin/optimize/research`，再运行 `bash scripts/start_weekly.sh`。
   也可直接运行 `python scripts/weekly.py start`。本地有改动、他人持锁或历史分歧时停止，不能继续实验。
2. 成功后读取 GOALS.md、BACKLOG、EXPERIMENTS 和 `python -m stockai bt board`。
   脚本打印下个实验编号；由现有 `bt eval --record` 分配编号，不手工猜号。
3. 遵守 CLAUDE.md 的停止条件、单假设限制、护栏和验证集锁。先测试，再评测并记录失败和成功的变体。
4. 提交策略、实验记录、BACKLOG、scoreboard。周报写到忽略的 `report.md` 或仓库外。
5. `python scripts/publish_weekly.py report.md`：校验编号、普通推送、复用/建立 PR、REST 追加周报、释放锁。
   同一提交和同一周报重试不会重复评论。推送遇到远端新提交则停止，绝不强推或自动合并重复编号。

PR 始终由所有者决定何时合并。GitHub 合并后 PR 会关闭，无法让同一个 PR 永久保持打开；下一轮仍沿用研究分支，自动建立承接 PR。
即使合并时删除了远端研究分支，下次从包含研究记录的 main 恢复；旧本地提交仍保留，分歧需人工核对。

## 中断和恢复

- 失败时保留远端锁、本地 `.git/stockai-weekly-lock.json` 和所有本地改动/提交。修复后直接重试发布。
- 如需重新同步启动步骤，先保存并提交改动，再 `python scripts/weekly.py start --resume`。
- 明确放弃会话时，先备份/推送需要保留的成果，再 `python scripts/weekly.py release`。该命令只释放本地记录所属的锁，不删除实验文件。
- 锁不按时间过期，防止慢会话仍在研究时被抢占。容器已销毁且本地锁记录丢失时，所有者先确认原会话彻底停止，再运行 `python scripts/weekly.py force-release --yes`：它在锁分支上追加一条 RELEASED 提交（原锁提交保留在历史里），不删除任何东西。不要手动删除锁分支。
- main 合并冲突：持锁解决，测试、提交后继续；不要用 checkout -B、reset 或 force push 覆盖历史。
- 两份历史已产生同号实验时：保留两边策略和原始输出，按时间/假设逐条核对，重编号并同步 BACKLOG、scoreboard；不能把 JSON 记分牌直接选一边覆盖。

## 定时会话提示词

为 stock-ai 做每周规则策略研究。初始分支必须设置为 optimize/research，新容器从该分支克隆。已有工作区先检查 git status --porcelain，若有本地改动先停止并报告；否则 git fetch origin、git switch optimize/research、git merge --ff-only origin/optimize/research，然后运行 bash scripts/start_weekly.sh。任一步失败就停止，不重置、不强推、不另开分支。必须成功取得会话锁并同步研究分支后才能开展研究；他人持锁就结束本次运行，不删除他人锁。随后读取并严格遵守研究分支上的 CLAUDE.md 和 GOALS.md，读取最新实验、BACKLOG 和记分牌，检查停止条件，只验证一个假设并记录全部变体。测试通过后提交，在同一条研究分支上运行 python scripts/publish_weekly.py report.md，将周报追加到长期 PR。失败时保留锁和成果并报告具体错误。不要合并 PR。
