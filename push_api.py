#!/usr/bin/env python3
"""通过 GitHub Git Data API 推送本地改动到远端 main。

背景
----
沙盒到 github.com 的 git / https 协议被网关拦成 403（带 token 也一样），
只有 api.github.com 通，所以推送只能走 Git Data API。

但 API 的 tree 更新是**整文件替换**，不带三方合并与冲突检测：
只要 ref 能快进（parent 取的是远端最新提交），本地某个文件的全文就会
直接顶掉远端同名文件——如果远端该文件已被别人改动，改动会静默消失。
因此本脚本加了四层防护（见 main() 里的注释）。

用法
----
  【默认 · 每次新建分支】推 → 自动合并 → 删分支，一条命令走完
  python3 push_api.py                 # 新建分支 → 推 → 开 PR → squash 合并 → 删分支
  python3 push_api.py -b feat/login   # 指定分支名（不给就自动生成 task/时间戳-随机）
  python3 push_api.py --hold          # 先不合并，保留分支攒改动
  python3 push_api.py --merge         # 合并 --hold 留下的 PR
  python3 push_api.py --allow-stale-merge   # 主干已前进且动了同文件时仍要合并
  python3 push_api.py --method rebase # 换合并方式（默认 squash）

  【分支体检】只报告，不删任何东西
  python3 push_api.py --prune             # 列出所有任务分支的状态和建议
  python3 push_api.py --audit             # 看最近 20 次运行发生了什么（--audit N 指定条数）
  python3 push_api.py --delete-branch 名字 # 真正删除（逐个确认）
  python3 push_api.py --branch 名字 --close-pr
  python3 push_api.py --status

  【三方全景】谁动了哪些文件，一次列全（只读）
  python3 push_api.py --scope                 # MINE/THEIRS/BOTH/LOCAL_ONLY/REMOTE_ONLY/UNKNOWN
  python3 push_api.py --scope --json          # 结构化输出（AI / CI 读归属的唯一可靠途径）
  python3 push_api.py --scope --mine          # 只列可直接推的路径（MINE + LOCAL_ONLY，一行一个，可管道逐个推）

  【直推主干】旧行为，四层防护照旧
  python3 push_api.py --direct

  【通用】
  python3 push_api.py --dry-run / --yes / -m "信息" / a.js b.js
  python3 push_api.py --pull [文件...]  # 三方合并主干改动到本地
  python3 push_api.py --resolve 文件    # 标记冲突已手工解决
  python3 push_api.py --init-baseline / --reset-baseline / --mark-synced
  # N297（第八十七轮，中危）：下面这行原先写的是
  #   --force-file 文件 / --force-overwrite   # 只有 --direct 才需要
  #
  # 「只有 --direct 才需要」是**错的**。实测（PR 工作流 = 默认，未加
  # --direct；基线未记录 b.txt、远端已有）：
  #   A. 无 --force-file → pushed: []
  #      ❌ b.txt: 基线里没有这个文件、远端却已存在，无法确认远端原状态
  #      "确知本地确实改过时，用 --force-file <文件> 逐个点名放行"
  #   B. 加 --force-file b.txt → pushed: ['c0000…']
  #
  # **同一条命令，加与不加决定能否推送。** 而 FileBaselineGuard.check()
  # 的 `if ctx.wf == "pr":` 分支里，unrecorded 那一类的出路恰恰就是
  # --force-file —— 两处代码都在 **PR 分支里**，不在 direct 分支。
  #
  # 文档把唯一能解封的出路说成"另一个模式下才需要"，用户会以为
  # 默认工作流下没有解法，从而卡死或误用 --force-overwrite（后者是
  # **全局**放行，会把 revert 类也一起放行 —— 覆盖面大得多）。
  python3 push_api.py --force-file 文件     # 逐个点名放行（PR 与 direct 都需要）
  # M175（A156，低）：上面第 59 行警告 --force-overwrite「覆盖面大」时
  # **只举了 revert 类**，紧邻的下一行又承诺「删除侧只归 --force-delete」
  # —— 而在 N595 修好之前，删除侧恰恰被 --force-overwrite 放行：三处
  # 调用点写成 `bool(force) or force_delete`，`bool(force)` 在 or 左边，
  # 实测 `--direct --yes --force-overwrite`（**没有** --delete）照样删。
  #
  # 两行相邻、同指一个开关、一起说错同一件事，而用户读到的正是这两行。
  # 现在这一半已由 `_deletes_forced()` 修好（只认 --force-delete），
  # 承诺与实际重新对齐；保留这段说明是为了让"覆盖面"这句话今后被改动时，
  # 删除侧这一维不再被漏掉。
  python3 push_api.py --force-overwrite     # 只放行「覆盖他人改动」（覆盖类，慎用）
  python3 push_api.py --force-delete        # 只放行删除侧的判据（不是 --force-overwrite）
  # N409（第一百三十一轮，低危）：上面这两个 "force" 与**删除熔断**的关系
  # 原先一字未提，用户很自然会以为"加了 force 就都放行了"。
  # 实测（plan_removals(..., force=True)，30 个真删除）：熔断照样 abort，
  # entries 被清空 —— 三者是**互相独立**的判据：
  #   · --force-overwrite  只放行「覆盖他人改动」这一类文件级判据
  #   · --force-delete     只放行删除侧的判据（如「远端被他人改过」）
  #   · 删除熔断（条目数 / 占比）不认任何一个 —— 它是按规模兜误判的，
  #     规模错了（切错 --root、用错基线、git 读取失败）时放行就是整仓删除
  # 一句话记：**--force-overwrite 与 --force-delete 都不绕过删除熔断**。
  # 所以熔断拦下时，出路只有核对规模后调 PUSH_API_DELETE_MAX_FILES /
  # PUSH_API_DELETE_MAX_RATIO，加 force 没用。

  【覆盖默认配置】脚本默认按当前 git 仓库推断，推错了目的地是最难排查的问题
  python3 push_api.py --root /path/to/repo   # 覆盖本地仓库目录
  python3 push_api.py --repo owner/name      # 覆盖远端仓库
  python3 push_api.py --state /path/to.json  # 每个仓库各自的基线文件

为什么默认走 PR
----------------
  把「互相覆盖」交给 GitHub 的 merge 判定：合并时它做真正的**三方合并**
  （以 merge base 为基准）。两人从同一基点各自改动 → 自动合上，或明确报
  409 冲突，都不会静默丢改动。

  每次推送都新建分支，是为了让分支基点 = 当前主干最新。基点越新，PR diff
  越小，三方合并的冲突面也越小；反过来，一个活了很久的分支基点会停在
  建分支那一刻，diff 越滚越大，人工介入越多就越容易出错。

  有一种情况 PR 也救不了：本地副本落后于主干，分支却是从最新主干建的。
  这时你的旧内容相对分支基点等于「把主干改动改回去」，GitHub 会当成你
  故意回退并直接采纳。所以「本地副本落后」**依然拦截** —— 先 --pull。

合并阶段那一段窗口（第四层）
--------------------------
  上面那些防护（本地副本过期、文件级基线、ref 乐观锁）**全部发生在推送
  之前**，而合并发生在推送之后：建 blob、建 tree 那几十秒里，另一台机器上
  的别人完全可能已经合并了别的 PR —— 本机的 flock 只管同机并发，管不到
  别人的机器。

  主干一前进，PR 的 merge base 就不再是「你校验过的那个主干」，而你本地
  也没有那些改动。若不检查，结果就是：你的改动 + 别人的改动自动合成一个
  「谁机器上都没跑过」的版本，脚本打印 ✅ 并刷新基线，全程零提示。他删了
  个函数你还在调，git 一声不响就合上了 —— 这叫「静默成功」，比报错危险。

  所以合并前补第四层：用 /compare 取「主干在基点之后改了哪些文件」，与
  「你推了哪些文件」求交集。

      主干改的文件 ∩ 我推的文件          结果
      --------------------------------  ----------------------------
      空                                你推的文件没人碰，放行（clean）
      非空                              拦下，保留分支与 PR（overlap）
      拿不到完整依据                    按「可能有交集」拦（unknown）

  选文件交集而不是「分支不够新就一律拦」，是因为后者太粗 —— 主干改了
  README.md 而你改的是 a.js，拦下只会把人推向 --force-overwrite。

  确认无需本地验证时，加 --allow-stale-merge 放行这一层；它不绕过推送前
  的任何校验（这层防的是「未验证」，不是「丢改动」）。

分支体检（--prune）
------------------
  判据是「合并状态 + 活动状态」，不是「N 天没用」：
    已合并 > 3 天，分支却还在 → 报告（正常流程早该删了）
    已合并 ≤ 3 天              → 静默（删除流程多半还在跑）
    PR 仍开启                  → 报告并保留（可能是进行中的工作）
    PR 已关闭 / 孤儿分支        → 报告，给删除命令
  一个开发到一半的 WIP，恰恰就是「开着、没合并、好几天没动」，
  按时间自动删会最先误杀它 —— 所以 --prune 只报告，删除一律走
  --delete-branch 并逐个确认。

同机并发
--------
  同一台机器上同时跑两个本脚本，第二个会**等待**第一个跑完（基线文件有锁），
  不是并行、也不是报错。锁会持有到整次推送结束，等待时屏幕上有提示。
  多人各用一台机器则不受影响 —— 那种并发由 GitHub 的合并机制保证。

约定
----
  · 基线状态写在 <root>/.git/push-sync.json（仓库内 .git 下，不入库、不串档）
    （旧版在 /data/workspace/.push-sync.json，现已迁移；可用 --state 覆盖）
  · 首次使用必须先跑 --init-baseline，否则推送会被拒绝
  · token 从环境变量 GITHUB_TOKEN 读取（禁止写进脚本）；
    传给 curl 时走 --config 临时文件，不出现在进程列表里
  · --dry-run 会做大文件预检并给出警告，但不推送、不建分支
  · 分支不是备份：改动存在本地工作区，合并失败时收掉分支不会丢东西

其它可用选项
------------
  下列选项此前**未出现在 --help**（N135）：能解析但用户无从得知，
  等于不存在。写进文档的选项必须能解析（N102），反之亦然。

  python3 push_api.py --ignore-pending       # 忽略未解决的冲突标记直接推
  python3 push_api.py --new-task             # 强制新建任务分支（不用 --hold 攒的那个）
  python3 push_api.py --no-delete            # 强制关闭删除（压过一切，CI 兜底用）
  python3 push_api.py --api                  # 强制走 API 通道（不用 git 推送）
  python3 push_api.py --keep-on-conflict     # 合并冲突时保留分支，不自动收摊
  python3 push_api.py --clean-conflicts      # 清理冲突副产物（含远端明文）
  python3 push_api.py --audit [N]            # 查看最近 N 条运行记录（默认 20）

  N335（第一百〇四轮，中危）：下面这批是**真能解析**但 --help 里没有的。

  ⚠️ 上一版这里凭空造出了 7 个**不存在**的选项
  （--exit-code / --version / --stdin / --max-time / --no-tags /
   --index-info / --show-toplevel）——它们是 git / curl 的**子进程参数**
  （`_grun(["fetch", "--no-tags", ...])`、`git("rev-parse",
  "--show-toplevel")`、`curl "-sL" "--max-time"`），被一条自身有缺陷的
  扫描断言当成脚本选项扫了出来。实测全部「未知参数」、退出码 1。
  而守着它的测试只 grep 字符串、不验证能否解析 → 13 项全 PASS。

  --help 是**唯一的选项权威来源**，往里写不存在的东西比漏写更糟：
  用户照着敲，得到一个"未知参数"。
  python3 push_api.py --json                 # 结构化输出（目前只在 --status / --scope 完整可用）
  python3 push_api.py --delete               # 开启删除（默认关闭，删除不可逆）
  python3 push_api.py --stale-days N         # --prune 判定「多久算僵尸分支」
  # M52（第一百〇七轮，中危）：上面这行原本写的是
  #   --allow-overwrite  # 放行「会覆盖他人改动」的文件
  # 而那是 **--force-overwrite** 的语义（全局开关、不带参数）。
  # --allow-overwrite 实际是 --force-file 的**别名**，必须带文件名：
  #   elif a in ("--force-file", "--allow-overwrite"):
  #       opts["force_files"].append(argv[i + 1])
  # 用户照抄会拿到「--force-file 缺少参数」——报的还是另一个选项的名字。
  #
  # 判据 15 早就抓到它了（"用法行没给带值选项的参数位"），但它设为
  # 「报告，不失败」，于是 run_all 照样全绿。
  python3 push_api.py --allow-overwrite 可疑文件 要推的文件   # = --force-file
  python3 push_api.py --force-overwrite    # 只放行「覆盖他人改动」（覆盖类，慎用）
  #   M175（A156，低）：这一行原先写「全局放行」，与紧邻的下一行
  #   （--force-delete 只放行删除侧）自相矛盾 —— 在 N595 修好之前，
  #   删除侧恰恰被 --force-overwrite 放行。上面第 70 行那一处已改，
  #   这里是**同一句的第二个落点**，一并改，否则按行号修只修到一半。
  python3 push_api.py --quiet                # 压掉 Ui 通道的 info 级进度噪音，警告/错误照常输出
  #   ⚠ 覆盖面有限（M55，A59 实测）：脚本大量输出直接走 print，**不经 Ui 通道**，
  #     --quiet 对它们无效（实测 557 处输出里只有 1 处真正被它压掉）。
  #     所以「开了 --quiet 但输出没变少」是**预期内的**，不是开关失灵 ——
  #     排查时别先怀疑它。另：被 Guard 拦下的整段输出（为什么被拦 + 怎么解除）
  #     由 ui_keep_all() 强制保留，--quiet 也不会压（N343）。
  python3 push_api.py --message "信息"       # 与 -m 等价
"""
import base64
import calendar
import contextlib
import difflib
import fcntl
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

try:
    import signal as _signal
except ImportError:                                  # pragma: no cover
    _signal = None                                   # 非 POSIX（Windows）

# 删除 / 重命名支持（可选插件）。缺失时降级为「不处理删除」，
# 而不是让整个脚本起不来 —— 删除是可选能力，不该成为启动的硬依赖。
#
# ⚠ 安全：插件**默认不加载**。
#
# 本脚本是**单文件形态**，用法就是拷进目标仓库里跑。而 Python 的
# import 会先搜脚本所在目录（sys.path[0]）—— 于是那个仓库里只要有人
# 放一个同名的 delete_rename_support.py，它就会被当成插件加载。
#
# 实测：往仓库里放一个同名文件，脚本启动即执行了里面的任意代码
# （写文件、发请求、什么都行），且**伪装成可用插件**（提供同名函数），
# 主程序检测不到任何异常。
#
# 这不是理论风险 —— 913 文件的仓库有并行开发者，脚本又必须拷进仓库跑。
# 默认加载等于把「谁能往仓库里加文件」等同于「谁能在这台机器上执行代码」。
#
# 所以反过来：**默认关闭**，要用删除功能的用户显式开启，并自行确认
# 插件可信。开启后 --status 会打印插件的**实际路径**，让人看得见加载了谁。

_PLUGIN_DIR = (os.environ.get("PUSH_API_PLUGIN_DIR") or "").strip() or None
_PLUGIN_DIR_ABS = None
_PLUGIN_LOADED_FROM = None


_plan_removals = _report_removals = None
_sync_local_seen = None
_PLUGIN_ERROR = None


def _plugin_note():
    """删除/重命名的启用状态（供 --status / 启动提示使用）。

    原先这里报的是「插件从哪个路径加载」—— 那时代码来自外部文件，
    用户有权知道加载了谁。合并进单文件后没有外部加载了，
    报路径失去意义（且会误导人去找那个已不存在的文件）。
    """
    if _PLUGIN_ERROR:
        # 不要写成「不可用（未启用（默认关闭））」—— 嵌套括号
        # 读起来像报错，且原因重复两遍。
        return (f"删除/重命名支持未启用（{_PLUGIN_ERROR}）。"
                f"推送不受影响，但删除不会被处理。\n"
                f"   开启方式：" + _delete_how_to_enable().replace(
                    "\n", "\n      "))
    return "删除/重命名支持已启用（不可逆操作，会走熔断与人工核查提示）"

# Token 只从环境变量读取，不落盘、不入库。
#   导出方式：export GITHUB_TOKEN=github_pat_xxx
# 注意：旧版本曾把 PAT 明文写在本文件里，若该 PAT 曾被推送到公开仓库，
# 请立刻到 GitHub → Settings → Developer settings → Personal access tokens 吊销。
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
OWNER, REPO = "adwlachowicz684-star", "tauriTools"
BASE = f"https://api.github.com/repos/{OWNER}/{REPO}"
ROOT = "/data/workspace/tauriTools"
BRANCH = "main"
STATE_PATH = "/data/workspace/.push-sync.json"

# 编译期默认值快照 —— 只用于判断「有没有被外部改过」。
#
# 这四个常量硬编码了某个具体仓库（tauriTools），而脚本本身放在另一个仓库
# （push-api-tools）里 —— 于是 cd 到脚本所在目录直接跑，操作的是**别的**
# 远端仓库。这是配置模型坏掉最直观的症状。
#
# 修法不是把常量改成另一个仓库名（那只是换个错法），而是让它能被覆盖。
# 但一次性改成显式传参要动 90+ 个函数，风险太大；所以阶段 0 先做
# 「可覆盖 + 自动推断」：启动时按 CLI > 环境变量 > git remote > 旧默认
# 的顺序定一次值，之后全文件照旧读模块级变量。
#
# 判定「有没有被外部改过」靠下面这组快照：测试会直接赋值
# `mod.ROOT = ...`，那时它不等于默认值，resolve_config 就不去覆盖它 ——
# 否则配置推断会把测试搭好的沙箱冲掉，全量回归当场红一半。
_DEFAULT_OWNER, _DEFAULT_REPO = OWNER, REPO
_DEFAULT_ROOT = ROOT
_DEFAULT_STATE_PATH = STATE_PATH
_DEFAULT_BASE = BASE

#: N417（第一百三十五轮，中危）：记录**本函数上一次实际写入的值**。
#:
#: 「当前值 != 编译期默认常量」这个判据区分不了两件事：
#:   · 外部（调用方 / 环境变量 / 测试沙箱）显式设过 → 不该覆盖 ✅
#:   · **脚本自己上一次解析时设的**         → 必须覆盖 ❌ 原先不覆盖
#: 于是同进程内第二次 resolve_config 时，四项**全部残留**在第一次的值上，
#: 连 `detect_repo_slug` 都老实回答第一个仓库（它问的是残留的 ROOT）。
#:
#: 用「值是不是我们上次写进去的那个」来区分，而不是推翻
#: 「不覆盖外部显式设过的值」这条约定 —— 后者是测试沙箱的基础
#: （27 个测试文件里有 `mod.ROOT = ...` 这种写法）。
_LAST_RESOLVED_VALUES = {}


_DEFAULT_OF = {"ROOT": _DEFAULT_ROOT, "OWNER": _DEFAULT_OWNER,
               "REPO": _DEFAULT_REPO, "BASE": _DEFAULT_BASE,
               "STATE_PATH": _DEFAULT_STATE_PATH}


def _snapshot_pre_resolve():
    """记录本函数**动手之前**这五项的值。"""
    g = globals()
    return {_n: g.get(_n) for _n in _DEFAULT_OF}


def _reset_previously_resolved():
    """只复位「上次由我们**顶掉默认值**写入」的那些项。

    ⚠ 判据必须是「上次写入时**原值是默认值**」，不能只看
    「当前值 == 上次写入值」—— 后者会把测试沙箱显式设过的值也复位掉
    （27 个测试文件里有 `mod.ROOT = ...`，而 run() 会在同进程里反复调
    main()：第一次记下沙箱路径，第二次就把它复位成编译期默认路径，
    test_hardening 的冲突副产物用例因此整片变红）。
    """
    g = globals()
    for _name, _rec in list(_LAST_RESOLVED_VALUES.items()):
        _val, _over_default = _rec
        if _over_default and g.get(_name) == _val:
            g[_name] = _DEFAULT_OF[_name]
    _LAST_RESOLVED_VALUES.clear()

_CONFIG_INFO = {}              # resolve_config() 的结果，供 --status 展示

#: 仓库是不是**有依据**地定下来的（CLI / 环境变量 / git remote 推断成功）。
#:
#: 不能靠「当前值 != 编译期默认值」来判断 —— 那样**显式指定默认仓库本身**
#: 会被当成"没指定"，用户永远绕不过去（死锁）。见 _refuse_default_repo。
_REPO_EXPLICIT = False

#: M91（A96）：本次运行是否已被限流打光额度（remaining == 0）。
#: per-run 状态 —— 不复位的话，同进程第二次运行会继承上一次的"已限流"。
#: 用途：--pull 的合并循环对**每个文件**独立调 api()，额度尽后还会对
#: 剩下几百个文件各做一遍"重试 3 次 + 睡 120 秒"（实测 30 文件
#: sleep 3600 秒、限流提示 90 行；外推 835 文件 ≈ 27.8 小时）。
_RATE_LIMITED = False

#: N302：本次 `--prune` 里**分支列表**的截断标志。
#: prune() 后段在 PR 列表之后才读截断标志，而标志是全局单处
#: （_paginate.last_truncated）—— PR 触顶会把它置 True，PR 数远超
#: 分支数是常态，于是"分支列表被截断"这个判断长期为假真。
#: 分支列表拉完就存下来，后段复用它。复位清单必须含它（判据 5 会抓）。
_BRANCH_TRUNC = []
_BLOCKED_BY_GUARD = None       # 本次运行被哪道防护拦下（顶层据此决定退出码）

#: N362：本次运行被**取消**（含 CI 非交互取消）。
#:
#: 与 _BLOCKED_BY_GUARD 同族：两者都"什么都没推"，但处置不同 ——
#: 拦下是"按提示处理即可"，取消是"这次没跑成，需要重跑（可能要加 --yes）"。
#: 退出码 0 会让 CI 判成推送成功，而远端一个字节没变。
_LAST_RUN_CANCELLED = False
# M119（A108）：「没有需要推送的内容」用 raise _Cancel() 表达结束，
# 于是它走了**取消**通道 → _LAST_RUN_CANCELLED = True → 退出码 4。
# 而 4 的契约是「取消：这次没跑成，需要重跑」—— 无事可做不是取消：
# 基线该刷新的已刷新、分支已清理，重跑什么都不会变；CI 拿到 4 会按
# 契约加 --yes 重跑，实测重跑仍是 4。单独一个标志，两条取消通道不再
# 共用一个判据（仍复用 _Cancel 只为触发分支清理）。
_NOTHING_TO_PUSH = False
#: A6-J2：`--new-task` 时「上一个被接管的任务分支」。
#: 登记点与生效点分开：登记发生在建分支时，生效（从 tasks 摘除 + 打印）
#: 发生在**推送确实完成、状态文件即将写回**之前。取消 / 无事可做 /
#: --dry-run 都不该产生「已不再接管」这条声明。
_ORPHANED_TASK_OLD = None
#: main() 复位**之后**仍要能被顶层读到（N240）。
#: reset_per_run_globals 会清 _BLOCKED_BY_GUARD（同进程连跑用例需要），
#: 而退出码判定发生在复位之后 —— 所以值必须在**复位之后再写回**这里。
#: M42：它仍在复位清单里（跨运行不串味所必需），靠的是「复位之后再
#: 写回」而不是把它挪出清单。见 main() 的 finally。
#: 只在 main() 的 finally 里写、只在 __main__ 里读。
_LAST_RUN_BLOCKED = None

#: M126（A116）：本次运行是被**哪个信号**终止的（0 = 没有/是 Ctrl-C）。
#:
#: N480 给 SIGTERM / SIGHUP 装 handler 之后，它们也走 KeyboardInterrupt
#: 通道（这是唯一被清理路径显式重抛的类型 —— 自定义异常会被
#: `except BaseException: pass` 吞掉，进程继续往下跑而用户以为已退出）。
#: 于是顶层那一句「已中断（Ctrl-C）」会对 CI 超时 / 终端断开**说谎**，
#: 130 也不是 POSIX 惯例（应为 128 + sig，即 143 / 129）。
#:
#: 与 _LAST_RUN_BLOCKED / _LAST_RUN_CANCELLED 完全同族（M42 那一族）：
#: 它在复位清单里，所以必须"先取值 → 再复位 → 最后写回"，
#: 否则 main() 的 finally 会把它清成 0，顶层恒读到 Ctrl-C。
_LAST_TERM_SIGNAL = 0

#: 本次运行是否是 --dry-run（N246）。
#: 出口在 finally 里，拿不到局部的 opts；而它需要知道该不该
#: 为"重算待删除清单"发真实网络请求。
_DRY_THIS_RUN = False


def _dry_this_run():
    return bool(_DRY_THIS_RUN)


def _deletes_forced(opts):
    """N595 / M174（A156）：删除侧的放行**只**由 `--force-delete` 决定。

    三处 `plan_removals` 调用点原先都写成：

        force=bool(force) or opts["force_delete"]

    `force` 是 `--force-overwrite`。`bool(force)` 在 `or` **左边**，于是
    只要加了覆盖开关，删除判据就被整体放行 —— 不管有没有 `--delete`。

    M174 实测（真实 `_main()`，`--direct --yes --force-overwrite`，
    **没有** `--delete`）：tree payload 里 `x.txt` 照样被删。也就是说
    泄漏不是"叠加"，而是**替代**：用户没有任何删除意图的表达，只加了
    一个"我要覆盖远端"的开关，删除照样发生。

    N595 的覆盖面比 194 轮实证的更宽 —— 判据级实测三条判据**一起**被
    放行，不止"远端已被他人改动"那一条：

        ① 本地从未拥有过      force=True → entries=['y.txt']
        ② local_seen 不可用   force=True → entries=['y.txt']
        ③ 远端已被他人改动    force=True → entries=['y.txt']

    ① 最重：被删的文件在本地 git 历史里**根本没有副本**。
    而 `--force-delete` 本身工作正常（B 组对照），所以去掉 `bool(force)`
    不会让"该删的删不掉" —— 修这条不破坏 R4 已经补上的那一半。

    写成函数而不是在三处各写一遍 `opts["force_delete"]`：三处里有一处
    用的是 `.get(..., False)`、另两处是 `opts[...]`，口径本就不同；
    单一出口才能让"删除授权来自哪个开关"这件事只有一个答案。
    """
    try:
        return bool(opts.get("force_delete", False))
    except BaseException:
        return False


def _shq(s):
    """M176（A157，中）：给用户照抄的 shell 命令里，**每一个**插值都必须过这里。

    为什么必须有单一出口 —— 逐个补已经被证明会漏：

        N92   补了 `git commit -m "msg" -- <paths>` 的 path 引号
        M156  补了 pull 冲突里那条 `cp` 的三操作数
        …    而同一函数里相隔 13 行的 `git add -f -- {p}` / `git add -- {p}`
             到现在还是裸的；`msg` 被塞进 `"..."` 里也没有转义。

    A128 那份专项点名的就是：「不是某个转义写错了，是没有单一出口」。

    后果不是"命令失败"，而是**可执行任意命令**：文件名含 `$(touch PWNED)`
    时照抄就执行了（A157 实测 PWNED 被创建）。而这条建议出现的时机恰恰
    是用户最慌的时候（刚打完「远端已生效、本地补提交失败」），此时照抄
    是本能反应。路径来自仓库内容、msg 常来自 CI 拼的 PR 标题 —— 都属于
    **外部可控输入**。
    """
    return shlex.quote(str(s))


MSG_FALLBACK = "chore: 通过 API 推送本地改动"

STATE_VERSION = 2                     # 1=只记 sha；2=记 (mode, sha)

USAGE = __doc__          # --help 直接打印模块 docstring（用法段就在里面）

MAX_BLOB_BYTES = 10 * 1024 * 1024     # 超过这个大小直接拒推。
# N591（A153）：括号里原先写「应改走 Git LFS」—— 那是**死路**：
# 交给 LFS 后本脚本会**永久**拒绝推送该文件（见 SizeGuard 的
# not_recommended）。A117 改了 SizeGuard 的出路与文案，没回头改这行，
# 于是下一个读这行的人仍会把 LFS 当成官方推荐路径。
MAX_PREVIEW_LINES = 20000             # 预览用 diff 的行数上限，超了只报行数差
MAX_PREVIEW_BYTES = 256 * 1024        # 超过这个大小不拉远端内容做预览
VALID_MODES = ("100644", "100755", "120000")
VALID_MERGE_METHODS = ("merge", "squash", "rebase")   # GitHub 支持的三种合并方式
DEFAULT_TIMEOUT = 60                  # 默认 curl 超时（秒）

# ---- PR 工作流 ----
TASK_PREFIX = "task/"                 # 任务分支前缀（清理时按它识别）

# 长期分支：--prune 体检时排除（主干另由 BRANCH 排除）。
# 用集合而非前缀白名单，理由见 prune() 里的注释。
PROTECTED_BRANCHES = set(
    b.strip() for b in
    os.environ.get("PUSH_API_PROTECTED", "main,master,develop,release").split(",")
    if b.strip())
DEFAULT_WORKFLOW = "pr"               # pr=任务分支+PR；direct=旧版直推主干
MERGED_GRACE_DAYS = 3                  # 已合并但分支仍在，超过这个天数才报告
                                      # （防止刚合并、删除流程还在跑就被反复报告）

# ---- 合并前基点校验（第四层）----
# N615（第二百〇三轮，中危）：这里原本留着 `COMPARE_FILE_CAP = 250`，
# 注释写着"保留**仅供测试**构造历史阈值场景"，并点名 test_merge_guard /
# test_merge_preflight_fail 仍按 250 造用例。两条证据**都不成立**：
#   · test_merge_guard 的 `range(mod.COMPARE_FILE_CAP)` 换成 `range(1)`
#     后全套仍 ALL PASS —— 那 250 个虚构文件对判据零影响
#     （`_merge_preflight` 只读 `cmp.get("merge_base_commit")`，
#      `files` 数组自 N230 起在生产侧零读取点）；
#   · test_merge_preflight_fail 的 `m.COMPARE_FILE_CAP = 250` 是给它赋
#     它已有的值，既不读也不改。
# 而它因此**删不掉** —— 后来者读到那段注释会以为"测试还依赖 250，不能删"。
# 这与 N230（死函数本体）是同一个危害、同一类受众：一个被注释主动论证过
# "为什么要留着它"的死常量。已删；两处测试改用字面量 250，并写明它与
# 任何阈值无关（真要构造"触顶"场景，靠的是 tree 的 truncated 标志）。


# ---------------------------------------------------------------- 基础

def _timeout_for(size_bytes):
    """超时按体积动态算：每 MB 加 10 秒，30 秒保底，10 分钟封顶。"""
    return min(600, max(30, 30 + size_bytes // (1024 * 1024) * 10))


class GuardBlock(SystemExit):
    """被**防护拦下**（区别于「出错」）。退出码 2。

    全部退出码用一个 SystemExit 子类表达，而不是散落 sys.exit(2)：
      · 顶层一处 except 就能统一转退出码，不用在 20 个拦截点各写一遍
      · 能被现有 `except SystemExit` 正常接住（它是 SystemExit 的子类），
        不会因为异常类型变了而穿透到裸 traceback
      · 语义清晰：抛它 = 「我拦住了，改动没丢」，而不是「我崩了」

    为什么必须区分（CI 场景）：
      退出码恒为 1 时，CI 无法区分「被防护拦下」/「参数错误」/「网络失败」/
      「改动真丢了」—— 前三种都不该触发告警，最后一种必须立刻告警。
      无法区分的结果是要么全部告警（噪音，久了就没人看），
      要么全部忽略（真出事时同样没人看）。

    退出码约定：
      0   成功
      2   被防护拦下（改动未丢，按提示处理即可）
      3   冲突未解决
      4   取消（含 CI 非交互取消；这次没跑成，需要重跑）—— N362
      1   其它错误
      130 用户中断（Ctrl-C）

    N241：全脚本 `exit(3)` / `code = 3` 出现 **0 次**（对比：
    sys.exit(130) 有真实现）。而 UnresolvedConflictGuard 的
    action="abort" → raise GuardBlock → code 恒为 2，
    与 SizeGuard（"文件过大"）同码。
    于是"冲突未解决"与"被防护拦下"在 CI 侧无法分开 ——
    契约专门为前者留了 3，却没有地方产生它。

    而这两者的处置方式完全不同：
      · 冲突未解决 → 需要人打开文件手工合并，机器做不了
      · 被防护拦下   → 跑 --pull / --mark-synced 就能自动解除
    CI 拿同一个码，只能要么都告警（噪音）要么都不告警（漏）。

    改成可覆写：GuardBlock(msg, code=3)。
    注意 SystemExit 的 code 是**实例**属性，类属性只是默认值；
    顶层 `sys.exit(getattr(e, "code", 2))` 读的是实例值。
    """
    code = 2

    def __init__(self, msg="", code=None):
        # SystemExit 的构造签名：SystemExit(code) —— 传字符串时
        # code 就是那个字符串、退出码为 1。而本类需要"消息 + 退出码"两个
        # 东西，所以显式收 code 并存成实例属性（覆盖类默认值）。
        super().__init__(msg)
        if code is not None:
            self.code = int(code)



def _blocked(msg, code=2):
    """N361：抛「被防护拦下」（退出码 2），而不是裸 SystemExit（退出码 1）。

    全脚本有 15 处「拦下」语义用的是 `raise SystemExit(...)`，退出码 1 ——
    与「参数错误」「网络失败」同码。而 `GuardBlock` 的 docstring 把理由
    写满了：

    > 退出码恒为 1 时，CI 无法区分「被防护拦下」/「参数错误」/「网络失败」/
    > 「改动真丢了」—— 前三种都不该触发告警，最后一种必须立刻告警。

    最不该混的两类被混成了同一码：`_refuse_default_repo`（推上去会落到
    **另一个仓库**，而本地看不出任何异常）与"拼错一个选项"，在 CI 里
    返回值完全相同。

    为什么用 helper 而不是各处直接 `raise GuardBlock(...)`：
    15 个调用点各写一遍迟早漂移（有的忘传 code），而 helper 只有一处。

    ⚠ 注意形态：**调用即抛**，所以调用点**不写** `raise`。
    写成 `raise _blocked(...)` 的那个 raise 永远执行不到（函数内部先抛了），
    而且读起来像"raise 一个返回值"，会误导后人"补上"一个 raise。
    """
    raise GuardBlock(msg, code)

class _Cancel(Exception):
    """正常取消：不推送，但必须走与失败相同的分支清理路径。

    为什么不用 return：分支在 create_branch() 时就已经建好了，
    而下面还有多处「发现问题 → return」的提前退出。每处 return 都会
    把那个空分支留在远端，本地 tasks 里却没有记录 —— 只能靠 --prune
    偶然发现。实测最高频的一条：用户在确认提示按 N，100% 留僵尸分支。

    改成抛异常后，取消与失败走同一条清理通道，新增退出点时也不会忘记。
    """


def _cancel_blocked(tag):
    """N566（第一百八十五轮，中危）：**被防护拦下**时走 `_Cancel` 通道。

    为什么不是直接 `raise GuardBlock(...)`：GuardBlock 不会走 `_Cancel`
    那条分支清理通道，而这条路径上远端分支**已经建好了**
    （create_branch 在 Guard 之前）—— 直接抛 GuardBlock 会留下僵尸分支。

    为什么又不能只 `raise _Cancel()`：那是**取消**通道，顶层据此
    `sys.exit(4)`，而 4 的契约是「这次没跑成，需要重跑（多半加 --yes）」。
    而这里是**脚本主动拦下**（不是用户按 N）：

      · 加 `--yes` 无效 —— 实测 revert / unrecorded / blocked 三处
        全带 `--yes` 仍是 4，重试永远不会成功（远端那个改动还在）；
      · 输出里**零「已取消」字样**，写的是「先跑 --pull」；
      · 同样的"被防护拦下"，用 `GuardBlock` 的那几处给的是 **2**
        （「改动没丢，按提示处理即可」），用 `_Cancel` 的这三处给 4 ——
        同一个语义两个退出码，取决于 Guard 作者写了哪一句。

    所以：置位 `_BLOCKED_BY_GUARD`（顶层据此 exit 2，且排在 cancel
    之前判），再照旧 `raise _Cancel()` 走清理。两件事都要，缺一不可。

    N565（第一百八十五轮，中危）是这条的**同一处**：M104 只把 dry-run
    "能推"那半从 4 修成 0，"不能推"那半（预检发现会被拦下）仍是 4 ——
    它走的就是这三个出口。上面修完之后，dry-run 预检被拦下也落 2，
    不再需要 N565 单列的最小修法（对 dry 置 `_DRY_RUN_ONLY`）。

    ⚠ 形态：**调用即抛**，调用点不写 `raise`（与 `_blocked()` 同）。
    """
    global _BLOCKED_BY_GUARD
    if _BLOCKED_BY_GUARD is None:
        _BLOCKED_BY_GUARD = tag
    raise _Cancel()


def _flock_is_contention(e):
    """N312：flock 抛的 OSError 是不是"被别人持有"（而不是锁机制不可用）。

    只有 EAGAIN / EACCES / EWOULDBLOCK 表示"锁已被占用"—— 那才是并发，
    值得等。其余全是**锁机制本身不可用**，等也没用：

        ENOLCK  内核锁资源耗尽（NFS / 容器里常见）
        EINVAL  该 fd 不支持 flock（比如某些特殊文件）
        EBADF   fd 已失效
        EINTR   被信号中断

    把这几类也说成"另一个进程在跑"是**归因错误**：用户会去 ps 找进程、
    等、重跑，方向完全跑偏（N79 只对 os.open 做了这个区分，flock 内部
    一直是混的）。
    """
    import errno as _errno
    code = getattr(e, "errno", None)
    return code in (_errno.EAGAIN, _errno.EACCES, _errno.EWOULDBLOCK)


def _flock_unavailable_msg(e, path):
    """M78（A82）：「锁机制本身不可用」的文案 —— 两处 flock 出口共用。

    N312 只把归因区分贴在**第一次** flock（LOCK_EX|LOCK_NB）上，
    第二次（LOCK_EX 阻塞等待）的 `except OSError as e2` 照旧一律说
    "另一个 push_api 进程正在跑"。

    而走第二次的用户**已经等过一轮**（正是因为第一次判为并发才进来的），
    最容易照提示继续等下去 —— 比第一次就误导更糟：

        EAGAIN → ENOLCK    另一个进程 ★   锁机制不可用 ✓
        EAGAIN → EBADF     另一个进程 ★   锁机制不可用 ✓
        EAGAIN → EAGAIN    另一个进程 ✓   另一个进程 ✓（未受影响）
        ENOLCK →（不触发） 锁机制不可用 ✓ 锁机制不可用 ✓

    文案抽出来共用，而不是两处各写一份：N312 的教训就是
    「同一份事实只落实了一部分」，再抄一份等于再埋一次。
    """
    return (
        f"无法锁定 {path}：{e}\n"
        f"  这是**锁机制本身不可用**，不是有另一个进程在跑：\n"
        f"    · ENOLCK —— 内核锁资源耗尽（NFS / 容器里常见）\n"
        f"    · EINVAL / EBADF —— 该文件系统不支持 flock\n"
        f"    · EINTR —— 被信号中断\n"
        f"  换个目录放基线文件：--state <本地路径>/push-sync.json")


class StateLock:
    """同一台机器上多个进程并发跑本脚本时，保护 .push-sync.json。

    基线是单机共享的，两个进程同时读写会互相覆盖：后写的那个会把先写的
    base_commit / synced_commit 冲掉，导致其中一个进程拿着过期基线继续判定，
    文件级防护随之失效。

    锁住让它们串行，反而更安全：脚本只用 `git add -- <本次文件>`，
    先跑完的那个不会卷走另一个人在工作区里的改动。

    **锁的持有范围是整个 _main()，包含全部网络请求** —— 建 blob、建树、
    建提交、开 PR、合并都会持锁。一次推送慢的话要等几分钟。
    这里刻意选了「简单且绝对安全」而不是「细粒度但容易写错」：
    基线的读→判定→写必须原子，拆成多段短锁反而会重新引入竞态。
    等待时会打印提示，不会无声卡住。

    注意：flock 只在**同一台机器**上有效。多人各用一台机器时不需要它 ——
    那种并发由 GitHub 的合并机制保证（见 merge_pr 的注释）。
    """
    def __init__(self, state_path, enabled=True, quiet=False):
        self.path = (state_path or STATE_PATH) + ".lock"
        self.enabled = enabled
        # M65（A73）：`quiet` 用于纯数据流模式（--json / --mine）。
        # 本锁在 `_main()` **之前**执行，属 N99 那条判据的管辖范围
        # （"分派前的任何输出都必须静音"）—— 否则 JSON 消费者会拿到
        # "前面几行文本 + 末尾一个 JSON"，解析不了。
        #
        # ⚠ 本条曾被 PR #217 整体覆盖而**静默回退**（A99 复核发现）。
        self.quiet = quiet
        self.fd = None

    def __enter__(self):
        if not self.enabled:
            return self
        # N79：os.open 失败与 flock 阻塞失败是**两种成因完全不同**的失败，
        # 原先包在同一个 try 里、共用一句报错。
        #   · os.open 失败 = 目录不存在 / 权限不足 / 只读盘 → 配置或环境问题
        #   · flock 失败   = 另一进程持有锁 → 并发（"另一个进程在跑"才适用）
        # 实测：路径不存在时报的是"另一个 push_api 进程正在跑"，
        # 排查方向完全跑偏（且它先于仓库校验执行，踩中的概率不低）。
        try:
            # 0600：与状态文件同权限（状态文件已改 0600）。锁文件本身不
            # 含敏感信息，但 0644 意味着同机其他用户能看到/推测仓库布局。
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as e:
            _dir = os.path.dirname(self.path) or "."
            raise SystemExit(
                f"无法创建锁文件 {self.path}：{e}\n"
                f"  这是**环境或配置问题**，不是并发：\n"
                f"    · 目录不存在（{_dir}）—— 检查 --state / --root 是否指向了错误位置\n"
                f"    · 目录无写权限或只读挂载\n"
                f"  确认基线文件路径：python3 push_api.py --status")
        # 先试非阻塞：抢不到就打一行提示再阻塞等待。
        # 锁会持有到整次推送结束（含全部网络请求），等待可能长达数分钟；
        # 一声不响地卡住会让人以为进程挂了，从而 Ctrl-C 或重复启动。
        # N312（第九十三轮，中危）：原先这里有两个 `except OSError`，
        # 第一个是**裸**的、排在前面 → 第二个**永不被执行**（死代码，
        # AST 扫描确认）。而两个的措辞都把**任何** OSError 说成
        # "另一个 push_api 进程正在跑"。
        #
        # flock(2) 还可能返回 ENOLCK（内核锁资源耗尽，NFS 上常见）、
        # EINVAL、EBADF、EINTR —— 这些与并发**毫无关系**。用户照提示去
        # `ps` 找进程、等、重跑，方向完全跑偏。
        #
        # 这是 N79 同一种病、深一层：N79 把 os.open 与 flock 分开了
        #（前者=环境/配置，后者=并发），但 **flock 内部**仍把"抢不到"
        # 和"锁机制不可用"混为一谈。
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if not _flock_is_contention(e):
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None
                raise SystemExit(_flock_unavailable_msg(e, self.path))
            # M65（A73）：这三行**必须 flush**。
            #
            # 它要防的那个现象，恰好就是它自己的症状 —— StateLock 自己的
            # 注释写着"一声不响地卡住会让人以为进程挂了，从而 Ctrl-C 或
            # 重复启动"。而实测：stdout 为**管道**时（块缓冲），
            # 进程正阻塞在 flock 上，缓冲区永远等不到 flush，
            # 提示**根本不显示** —— 正是一声不响地卡住。
            #
            #     stdout 为管道（默认块缓冲） → 提示不可见 ★
            #     python3 -u（无缓冲）        → 提示可见 ✓
            #
            # 而 CI / `| tee` / `> log` 下 stdout **恒为管道或文件** ——
            # 最需要这个提示的场景，恰好是它不显示的场景。
            if not self.quiet:
                print(f"  ⏳ 另一个 push_api 进程正在运行，"
                      f"等待它释放 {os.path.basename(self.path)}…",
                      flush=True)
                print("     （同一台机器上并发推送会串行执行，这是刻意的："
                      "共享的基线文件不能被并发写坏）", flush=True)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            except OSError as e2:
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None
                # M78（A82）：第二次 flock 也要归因 —— 与第一次共用
                # `_flock_is_contention()` / `_flock_unavailable_msg()`。
                # 走这里的用户已经等过一轮（第一次判为并发才进来），
                # 若此刻其实是"锁机制变得不可用"却仍说"另一个进程在跑"，
                # 他会继续等 —— 等一个永远不会结束的条件。
                if not _flock_is_contention(e2):
                    raise SystemExit(
                        _flock_unavailable_msg(e2, self.path) + "\n"
                        f"  （第一次尝试判为并发、已进入等待；等待期间锁机制"
                        f"变得不可用 —— 继续等没有意义）")
                raise SystemExit(f"无法锁定 {self.path}：{e2}\n"
                                 f"  另一个 push_api 进程正在跑；等它结束再试。")
            # M65：同上。等待可能数分钟，等到之后必须立刻告诉用户
            # "可以继续了"，否则上面那句"正在等待"就成了最后一句、
            # 用户以为还在等。
            if not self.quiet:
                print("  ✓ 已获得锁，继续执行", flush=True)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        return False


def require_token():
    """缺 token 必须在发请求前就失败，而不是拿到 401 后靠猜。"""
    if not TOKEN:
        raise SystemExit(
            "缺少 GITHUB_TOKEN 环境变量。\n"
            "  用法：export GITHUB_TOKEN=github_pat_xxx && python3 push_api.py ...\n"
            "  （token 请勿写进脚本或提交到仓库）"
        )
    return TOKEN


@contextlib.contextmanager
def _auth_config():
    """把 Authorization 头写进 0600 的临时文件，交给 curl --config 读。

    token 直接拼进 curl 的 argv 时，同机任何用户 `ps aux` 都能看到它 ——
    和「把 PAT 明文写进脚本」是同一类泄露，只是暴露面从仓库变成了进程列表。
    改用 --config 后，argv 里只剩一个不敏感的临时文件名。

    选 --config 而不是 `-H @file`：后者要 curl ≥ 7.55，且 @file 语法在不同
    版本间语义有差异；--config 是长期稳定的接口。stdin 也留给 payload 用。

    文件放 /dev/shm（内存盘，不落磁盘），不可用时回退系统临时目录；
    权限 0600，退出即删。

    token 先过白名单，只放行 GitHub token 的真实字符集 [A-Za-z0-9_.-]。

    写入的配置行是 `header = "Authorization: Bearer {tok}"`。
    token 里一旦出现 `"` 就能提前闭合引号、出现换行就能开启新的一行 ——
    而 curl 配置是「每行一条指令」，那意味着可以注入 `output = /path`
    或 `url = attacker.host` 这类指令。

    第一版白名单是 `re.sub(r"[^\x21-\x7E]", "", TOKEN)`（所有可打印 ASCII），
    它拦得住换行，却放行 `"` `\` 反引号 `$()`。实测这些字符暂不足以造成
    实际注入（curl 一行只解析一条指令，且后续内容被丢弃），但这是**碰巧
    安全** —— 依赖 curl 具体的解析细节，换版本或换配置写法就可能失效。

    收紧到 GitHub token 的真实字符集后，引号闭合这条路从根上就没有了。
    实测各类 token 均不受影响：ghp_ / gho_ / ghu_ / ghs_ / ghr_ /
    github_pat_ 前缀，以及旧版 40 位十六进制。

    用「拒绝」而不是「清洗掉非法字符」：清洗后 token 变了，请求必然 401，
    用户只会看到「认证失败」而不知为何；直接报「可能复制时混入了换行」
    才可诊断。
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", TOKEN or ""):
        raise SystemExit(
            "GITHUB_TOKEN 含非法字符（可能是复制时混入了换行、引号或空格）。\n"
            f"  GitHub 的 token 只由 A-Z a-z 0-9 和 _ - . 组成（当前 "
            f"{len(TOKEN or '')} 个字符）。\n"
            "  请重新复制，注意不要带上换行或首尾空格。"
        )
    tok = TOKEN
    # N481（A116）：预检（isdir + access(W_OK)）挡不住**真实写入失败**。
    #
    # 实测（64KB tmpfs 填满后把 tmpdir 指向它）：
    #     File "...", line 688, in _auth_config
    #       fd, path = tempfile.mkstemp(prefix="pushapi-curl-", ...)
    #     OSError: [Errno 28] No space left on device
    # 裸 traceback 穿透 _auth_config → api() → 顶层，用户看不到
    # 「换用系统临时目录即可继续」。容器里 /dev/shm 默认 64MB，
    # 满盘 / inode 耗尽 / quota / 只读化都会命中。
    #
    # 失败就换下一个目录重试。目录用尽是真失败，那时该让异常冒出去 ——
    # 静默吞掉会让后续请求拿着一个不存在的 config 路径去跑，表现为更难的
    # 「curl 报 config 打不开」。
    #
    # M163（A150，中危）：**回退必须把「建文件 + 写内容」一起包住**，
    # 只包 `mkstemp` 等于没包。
    #
    # 实测（A150，/dev/shm 真实写满到 statvfs 剩余 0.00 MB）：
    #     probe /dev/shm mkstemp OK   ← 成功
    #     probe /dev/shm write FAIL: errno 28   ← 失败在这里
    #
    # 因为 `mkstemp` 建的是**零长度文件，不占数据块** —— 它在满盘的文件
    # 系统上照样成功。真正的 ENOSPC 发生在 `f.write` 的 flush/close
    # （Python 把异常归因到 `with os.fdopen(...)` 那一行，极易误读成
    # fdopen 本身失败）。旧写法把写入留在 `except OSError` 之外，
    # 于是**回退永远不会被触发**：裸 traceback 穿透到顶层、退出码 1，
    # 与修复前逐字相同。而本处注释点名的目标场景正是「容器里
    # /dev/shm 默认 64MB」—— 也就是这个实测场景。
    _dirs = []
    if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
        _dirs.append("/dev/shm")
    _dirs.append(None)      # None = tempfile 默认的系统临时目录
    _last = None
    for _d in _dirs:
        fd, path = None, None
        try:
            # M125（A116）：文件名带 pid。没有它就无法判断残留属于
            # 「已退出的运行」还是「此刻正在跑的另一个 push_api」——
            # 而这正是下面只报不删的前提。
            fd, path = tempfile.mkstemp(prefix="pushapi-curl-%d-" % os.getpid(),
                                        suffix=".cfg", dir=_d)
            os.chmod(path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None                 # 所有权已交给文件对象
                f.write(f'header = "Authorization: Bearer {tok}"\n')
        except OSError as _e:
            _last = _e
            # 换目录之前必须把这个半成品清掉：mkstemp 已建出零长度文件，
            # 留着就是下一轮 `_report_stale_auth_files` 会报出来的残留。
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            continue
        try:
            yield path
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return
    raise _last


def _auth_cfg_dirs():
    """M164（A150）：认证临时文件**可能落在**的所有目录。

    与 `_auth_config` 的候选顺序同源：/dev/shm 优先（tmpfs，不落盘），
    写不进去时回退到系统临时目录（`tempfile` 的默认，`dir=None`）。

    写成函数而不是常量：/dev/shm 是否存在/可读是运行期事实，模块导入时
    固定下来会在容器里判错。
    """
    _out = []
    for d in ("/dev/shm", tempfile.gettempdir()):
        if d and d not in _out and os.path.isdir(d):
            _out.append(d)
    return _out


def _report_stale_auth_files(stream=False):
    """M125（A116）：报出上一次运行**遗留**的认证临时文件（内含 PAT 明文）。

    N480 的 handler 只覆盖了「可被捕获的信号」。SIGKILL 无法被捕获，
    残留**必然**发生；而 /dev/shm 是 tmpfs，重启才清、会累积。

    ### 为什么只报不删

    直接删会**误伤并发运行**：另一个 push_api 正持有活文件时把它删掉，
    它下一次请求就是 401 —— 表现为「认证失败」，而根因完全看不出来
    （实测：造一个死 pid 残留 + 一个活 pid 残留，补丁版只报了死 pid
    那一个，活 pid 的没动）。

    判活用的是文件名里的 pid（见 `_auth_config` 的 prefix）。

    ### 扫哪些目录（M164 / A150）

    原先写死一个**单一目录常量**（`/dev/shm` 字面量直接写在扫描函数里），
    而 `_auth_config` 的候选目录是**两个**：
    /dev/shm 优先，写不进去就回退到系统临时目录。实测（A150）两个死 pid
    残留各放一个，扫描器只提到 /dev/shm 那个、`/tmp` 那个一行没有。

    两个后果缺一不可地叠加：

    1. `/tmp` 下的 PAT 明文**永远不会被报出** —— 而 M125 的存在理由
       （SIGKILL 拦不住、残留必然发生）在 `/tmp` 上同样成立，且 `/tmp`
       不像 tmpfs 那样重启即清。
    2. 给出的清理命令**只覆盖 /dev/shm** —— 用户照做之后 `/tmp` 里那份
       还在，却以为清干净了。**这是两害里更重的那一半**：一句让人以为
       已经处理完的话，比不报更容易让人不再去找。

    所以两个目录都扫，且清理命令**按实际发现残留的目录**逐条给。

    ### 扫哪些文件名（N587 / A154，低）

    191 轮只点了 `pushapi-hdr-` 与 `pushapi-raw-`，而脚本真实产出的三族是：

        pushapi-curl-<pid>-*     认证配置（**含 PAT 明文**）← 唯一被扫的一族
        pushapi-hdr-*            响应头
        pushapi-*                响应体原始字节（远端内容明文）
        pushapi-body-*           大请求体（payload 明文，>8MB 时才落盘）

    照 191 轮写的 `pushapi-raw-` 去扩会**一个都匹配不到**（真实前缀是
    `pushapi-`，`tempfile.NamedTemporaryFile(prefix="pushapi-")`）。
    `pushapi-body-` 装的是请求体，敏感度不低于响应体，191 轮完全没提。

    后三族没有 pid 前缀（判不了活），且正常路径上用完即 unlink ——
    只有被 SIGKILL / 异常打断时才残留，所以按"残留"报，但在命令里
    与 curl 那族分开给，避免用户一把删掉正在用的。

    ### 清理命令不能用 glob（N586 低 + M171 低）

    两重失效，缺一不可地叠加：

    1. **N586**：glob 会命中**活 pid** 的文件。脚本已经算出了哪些是活的
       （`stale` 列表就是为此算的），却在给命令时把这份信息丢掉、改用
       通配符 —— 照抄就是删掉并发运行正在用的文件。
    2. **M171**：残留堆积之后 `rm -f /dev/shm/pushapi-curl-*.cfg` 本身跑不动
       （307551 个残留实测 `/usr/bin/rm: Argument list too long`，E2BIG）。
       这恰恰是 M125 存在的那个场景 —— 需要清理时给的命令失效。

    所以：直接给出已算好的具体路径（不用 glob），且分批给。
    """
    me = os.getpid()
    stale = []          # 判得了活、确认是残留的（curl 那族）
    other = []          # 判不了活的三族（hdr / raw / body）
    for d in _auth_cfg_dirs():
        if not os.access(d, os.R_OK):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".cfg") and not n.startswith("pushapi-"):
                continue
            if n.startswith("pushapi-curl-"):
                try:
                    pid = int(n[len("pushapi-curl-"):].split("-")[0])
                except (ValueError, IndexError):
                    # 老版本（N480 之前）没有 pid 前缀 —— 判不了活，按残留报。
                    stale.append(os.path.join(d, n))
                    continue
                if pid == me:
                    continue
                if _pid_alive(pid):
                    continue              # 活着 = 另一次运行正在用
                stale.append(os.path.join(d, n))
            elif n.startswith(("pushapi-hdr-", "pushapi-body-")):
                other.append(os.path.join(d, n))
            elif n.startswith("pushapi-"):
                # 真实 raw 形态是 `pushapi-<8 位随机>`（无 pid、无扩展名），
                # 只有它才可能是响应体；hdr / body 已在上两支取走。
                other.append(os.path.join(d, n))
    if not stale and not other:
        return
    _lines = []
    _lines.append(f"\n  ! 发现 {len(stale) + len(other)} 个上一次运行残留的"
                  f"临时文件（内含敏感明文）：")
    for p in (stale + other)[:5]:
        _lines.append(f"      {p}")
    if len(stale) + len(other) > 5:
        _lines.append(f"      … 其余 {len(stale) + len(other) - 5} 个")
    _lines.append("     来自被 SIGKILL 打断的运行（该信号无法被捕获，"
                  "脚本拦不住）。")
    _lines.append("     脚本**不自动删**：并发运行时删掉别人的活文件，"
                  "对方下一次请求就是 401，而根因看不出来。")
    if stale:
        _lines.append("     确认无推送在跑后可清理（**已排除活进程**的文件，"
                      "可整段照抄）：")
        # 不用 glob：一是会误删活 pid（N586），二是残留多到一定程度
        # `rm -f … *.cfg` 自己就跑不动（M171，E2BIG）。分批给具体路径。
        for _i in range(0, len(stale), 20):
            _batch = " ".join(_shq(p) for p in stale[_i:_i + 20])
            _lines.append(f"       rm -f {_batch}")
    if other:
        # 这三族判不了活（文件名里没有 pid），照抄有风险 —— 明说。
        _lines.append(f"     另有 {len(other)} 个响应/请求体临时文件"
                      f"（**文件名不含 pid，判不了是否正在被使用**）：")
        _lines.append("       find <目录> -maxdepth 1 -name 'pushapi-*' "
                      "-mmin +60 -delete")
        _lines.append("      （-mmin +60 只删一小时前的，避免误删正在跑的）")

    if stream or _stream_active():
        # N585：json 模式下收进 warnings 字段（运载体是
        # `_json_pending_warnings()`，消费者是 show_status / 分派后
        # `_drain_json_warnings()` 的 stderr 兜底）。压成一行紧凑文本。
        _json_pending_warnings().extend(
            _compact_warn(_l) for _l in _lines if _l.strip())
        return
    for _l in _lines:
        print(_l)


def _pid_alive(pid):
    """进程是否还在。只看「能不能发 0 号信号」，不依赖 /proc。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                       # 存在但不属于本用户
    except OSError:
        return True                       # 其它情况一律按"活着"处理
    return True


def _install_term_handlers():
    """N480（A116，中危）：给 SIGTERM / SIGHUP 装 handler，让 finally 跑起来。

    `_auth_config` 的 docstring 写着「退出即删」，而它只对 SIGINT 成立 ——
    实测 SIGKILL / SIGTERM / SIGHUP 三种信号下，装 PAT **明文**的
    `/dev/shm/pushapi-curl-*.cfg` 全部残留（权限 0600，但 /dev/shm 是
    tmpfs，重启才清、会累积；容器里默认 64MB）。

    ### 为什么必须抛 `KeyboardInterrupt`，不能抛自定义异常

    清理路径上多处是 `except BaseException: pass`（有意为之 —— 不盖掉
    真正的失败原因）。自定义异常会被**吞掉**：进程继续往下跑，而用户
    以为已经退出了。实测对照（同进程，装 handler vs 不装）：

        none     SIGTERM   残留 /dev/shm=1 /tmp=1   ★
        handler  SIGTERM   残留 /dev/shm=0 /tmp=0   ✓

    `KeyboardInterrupt` 是脚本里**唯一**被显式重抛的类型
    （见 `_cleanup_failed_branch`），所以只有它能真正走到清理。

    ### 不变的那一半

    SIGKILL 无法被捕获（内核直接回收），残留**必然**发生 —— 这正是
    M125 要报残留的理由：装 handler 只覆盖了「可被捕获」的一半。
    """
    if _signal is None:
        return
    def _on_term(signum, _frame):
        global _LAST_TERM_SIGNAL
        _LAST_TERM_SIGNAL = int(signum)
        # 必须 raise 而不是 sys.exit：sys.exit 抛 SystemExit，
        # 会被清理路径上的 `except BaseException: pass` 吞掉。
        raise KeyboardInterrupt
    for _sig_name in ("SIGTERM", "SIGHUP"):
        _s = getattr(_signal, _sig_name, None)
        if _s is None:
            continue
        try:
            _signal.signal(_s, _on_term)
        except (ValueError, OSError, RuntimeError):
            # 非主线程 / 已被占用：装不上就不装，退回原语义，
            # 但不能因此让脚本起不来。
            pass


def _harden_mode(path, want):
    """收紧权限到 want，并**验证结果**。返回 (是否达到, 实际权限或空串)。

    为什么必须验证：实测在某些文件系统上（本项目沙盒的 virtiofs 即如此）
    chmod **静默无效** —— 调用成功、不抛异常，但权限仍是 0777。

    这直接击穿了本脚本的两处安全声明：
      · 状态文件「已收紧到 0600」→ 实际 0777，同机任何用户可读
      · 冲突副产物「创建即 0600」→ 实际 0777，而里面是**远端明文**

    注释里写了不等于做到了。不验证的话，脚本会一直宣称一个它并没有
    提供的保护，而用户无从察觉 —— 这正是本工具最该避免的「静默失败」。

    不因此中断：权限改不动通常不影响推送本身（且 usr 可能确实无权限），
    但必须**说清楚**，让用户自己判断风险。
    """
    try:
        os.chmod(path, want)
    except OSError:
        pass                       # 改不动不该阻断主流程
    try:
        got = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return False, ""
    return got == want, oct(got)


#: 权限收紧失败的提示只报一次，避免刷屏（同一次运行里原因都一样）
_HARDEN_WARNED = set()

#: M81-⑤（A85 复核）：json 模式下被收起来的权限警告（见 `_warn_mode`）。
#: 由 `show_status` 并入 payload 的 `warnings`，per-run，必须进复位清单。
_JSON_PENDING_WARNINGS = []


def _json_pending_warnings():
    """M81-⑤：json 模式下 `_warn_mode` 收集警告用的容器（进程级）。"""
    return _JSON_PENDING_WARNINGS


def _drain_json_warnings():
    """N542：取走并清空 json 模式下被收起来的警告（per-run）。

    生产者是「任何会写状态 / 副产物 / 审计的命令」，消费者原先只有
    `show_status` 一个 —— 其余 json 出口（--scope --json / --mine /
    --mark-synced --json / --pull --json …）收起来之后**无人取**，
    最后被 main 的 finally 里的复位清单清掉，警告永久消失。

    统一走本函数：每个出口取一次就消费掉，不重复、不遗漏。
    """
    try:
        _got = list(_JSON_PENDING_WARNINGS)
        _JSON_PENDING_WARNINGS.clear()
        return _got
    except BaseException:
        return []


def _compact_warn(line):
    """N544：收进 `warnings` 之前把人类文案压成一行紧凑文本。

    `warnings` 字段的定位是给机器读的（show_scope 的 docstring 反复
    强调「纯数据消费者不该被迫去剥人类文案」），而 `_warn_mode` 收起
    的是原本准备 print 的三行原文 —— 带 emoji 与缩进（实测
    `  ⚠️  …`）。机器拿到只能当 opaque string。

    只做两件保守的事：去首尾空白、去掉行首的警告类 emoji。正文里的
    关键信息（路径 / 期望值 / 实际值）一个字都不动。
    """
    _s = (line or "").strip()
    while _s[:1] in ("⚠", "⚡", "❗", "!"):
        _s = _s[1:].strip()
        if _s[:1] == "️":          # 变体选择符（⚠️ 的后半个）
            _s = _s[1:].strip()
    return _s


def _harden_once(path, want, tag):
    """首次创建时收紧一次权限，结果只提示一次。

    N117：状态文件在 save_state 里逐次收紧（它每次都重写），
    审计流水是**追加写**，不重写，于是从未收紧过。
    这里只需在文件刚出现时做一次。
    """
    if path in _HARDENED:
        return
    _HARDENED.add(path)
    _ok, _got = _harden_mode(path, want)
    _warn_mode(tag, path, want, _ok, _got)


def _warn_mode(tag, path, want, ok, got):
    """权限没收紧时告知一次。

    N455（第一百四十九轮，中危）：去重键原先是 **tag 常量**，不是路径。
    而 `_write_conflict_artifact` 对每个冲突文件用的是同一个 tag，
    且每个文件会产生两份副本（`.remote` / `.base`）——
    实测 3 个冲突文件 = **6 份远端明文**全部 0777，提示里却只出现 1 个
    文件名。用户会以为只需处理这一个，其余同样世界可读可写却无声。

    收紧动作本身是逐文件做的（`_harden_mode(p, ...)` 每个文件都调），
    **只有告知被 tag 吞掉** —— 与 N454 同一形态。

    改为按 (tag, path) 去重：每个未收紧的路径各占一行。
    同名路径重复调用仍只打一次，不会刷屏。
    """
    if ok or (tag, path) in _HARDEN_WARNED:
        return
    _HARDEN_WARNED.add((tag, path))
    _lines = [
        f"  ⚠️  {path} 无法收紧到 {oct(want)}（实际 {got}）。",
        "     本文件系统不支持 chmod（常见于 virtiofs / 某些挂载的"
        "网络盘），同机其他用户可能可读。",
        "     涉及敏感内容时建议把工作目录放到支持权限的文件系统上。",
    ]
    # M81-⑤（A85 复核，新增）：这三条也打在分派**之前**（load_state →
    # _harden_once → 本函数），实测 `--status --json` 的首行就是它：
    #
    #     ⚠️  /…/push-sync.json 无法收紧到 0o600（实际 0o777）。…
    #     {  ← JSON 从这里才开始
    #
    # CI 恰恰常跑在 virtiofs / 容器挂载上，也就是**最容易命中**的一类
    # 环境。而它是**安全警告**，不能像 3918 / 13303 那样单纯静音 ——
    # 静音等于让最需要知道的人看不到。
    #
    # 改走 `_json_pending_warnings()`：json 模式下收起来，由
    # show_status 并入 payload 的 `warnings`（那个字段本来就是给机器
    # 读的），机器照样拿得到，且不再打断解析。
    if _json_active():
        # N544：存进 warnings 字段的是给机器读的，压成一行紧凑文本
        #（去 emoji / 缩进），正文关键信息不动。
        _json_pending_warnings().extend(_compact_warn(_l) for _l in _lines)
        return
    # N386：--mine 与 --json 是同一类输出契约（`_stream_active()`
    # 的 docstring 逐字写了「混一行文案就被当成文件名」）。本函数打在
    # 分派之前（load_state → _harden_once → 本函数），实测在不支持
    # chmod 的文件系统上 `--scope --mine` 的管道里会多出这三行。
    #
    # --mine 没有 payload 可挂（show_scope 的 only_mine 分支直接
    # print 路径并 return），所以这里只能静音 —— 与 --json 走
    # pending_warnings 不同，那条路有个能读的出口。
    # 代价：这一次运行看不到该安全提示。它按 (tag, path) 去重且
    # 只在**本进程**内去重，下一次非 --mine 运行仍会打出来。
    if _stream_active():
        return
    for _l in _lines:
        print(_l)


# ==================== 运行审计流水（#7）====================
#
# 缺口：一次运行做了什么判定、放行了什么、开了几号 PR，**只活在 stdout 里**。
# 终端一滚就没了，出问题无从回溯。而「僵尸分支是这个设计的一等公民」
# —— 生命周期缺显式状态机（plan → pushed → pr_open → merged → cleaned），
# 所以失败恢复只能靠事后 --prune 巡检。
#
# 设计取舍：
#   · JSONL 追加写：一行一次运行，天然按时间有序，崩溃也不会损坏已有记录
#   · 放在状态文件**同目录**（<root>/.git/）：天然不串档，且不会入库
#   · 审计失败**绝不阻断**推送：它是记录手段，不是防护。
#     写不进去只提示一次 —— 反过来（写不进就中止）会把一个记录工具
#     变成新的故障点。
#   · 轮转：超过 AUDIT_MAX_BYTES 保留尾部，避免长期高频使用无限膨胀
#     （本工具每天上百次运行，一年就是几万行）

AUDIT_MAX_BYTES = 4 * 1024 * 1024        # 4MB，约 1~2 万条
_AUDIT_WARNED = False
_AUDIT_GUARD_DONE = False

#: M99（A101）：脚本**自己造出来的**运行时文件（状态文件 / 锁文件 /
#: 审计流水）是否已被本轮检查过。必须进 reset_per_run_globals()，
#: 否则同进程第二次运行不再提示（M59 那一族）。
_SELF_ART_GUARD_DONE = False


_RUN_ID = None


def _run_id():
    """本次运行的唯一 id。跨模块调用共享同一个，便于串联同一条流水。"""
    global _RUN_ID
    if _RUN_ID is None:
        _RUN_ID = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    return _RUN_ID


def audit(event):
    """记录一次事件（自动补 run_id / 时间戳 / 子命令）。

    event 是业务字段；公共字段统一在这里加，避免每个调用点各写一半，
    日后对不上。
    """
    global _AUDIT_GUARD_DONE
    if not _AUDIT_GUARD_DONE:
        _AUDIT_GUARD_DONE = True
        bad = _audit_guard()
        # N497（第一百二十四轮，中危）：本段是 --mine 管道里**唯一还在
        # 漏**的一处人类文案。
        #
        # N386 把分派前那四处提示都改成了 `_stream_active()`，实测
        # `--scope --mine` 的前两行却仍是文案：它们来自**这里**——
        # `audit()` 的守卫是「首次调用即打印」，而 --scope 在分派前
        # 就会记一条 guard_pass，于是守卫比 N386 修过的那四处更早落地。
        #
        # 后果与 N386 同形，只是更常见（任何没有 .gitignore 条目的仓库
        # 都会触发，不需要"基线残缺"这种特定前提）：
        #   --scope --mine | while read f; do push_api.py "$f"; done
        #   → 把「⚠️ 审计流水 push-audit.jsonl 未被 .gitignore 屏蔽…」
        #     当成文件名去推，两条都是不存在的文件。
        #
        # 静音而非跳过：守卫照样跑、照样只在进程内去重一次，下次非
        # --mine 运行仍会打出来（与 N386 的取舍一致 —— 纯数据流模式下
        # 让调用方的循环自然结束，比输出说明再被当成数据要好）。
        _quiet_audit = _stream_active()
        # M99（A101）：三个自造文件一起查，不只查审计流水。
        global _SELF_ART_GUARD_DONE
        if not _SELF_ART_GUARD_DONE:
            _SELF_ART_GUARD_DONE = True
            _arts = _self_artifact_guard()
            if _arts and not _quiet_audit:
                print("  ⚠️  脚本自己产生的运行时文件**未被** .gitignore "
                      "屏蔽，会被 detect_changes() 拾取并推上远端：")
                for _lab, _rel in _arts:
                    print(f"     - {_rel}（{_lab}）")
                print("     建议：把上面的文件加进 .gitignore，或用 --state "
                      "指向工作区之外（审计流水与状态文件同目录）")
        if bad and not _quiet_audit:
            # N320（第九十七轮，中危）：N303 换了判据 —— 现在"未跟踪且
            # 未被 ignore"才是主场景，而这句话还写着旧判据的**诊断**
            # 与旧判据的**建议**。
            #
            # 实测（未跟踪、未被 ignore —— 正是 #108 新增的主场景）：
            #   ⚠️ 审计流水 X 已被 git 跟踪，会随推送入库。
            #     建议：… 或 git rm --cached X
            #   → git rm --cached: fatal: pathspec 'X' did not match（128）
            #
            # 三点：
            #  ① 诊断是假的 —— 文件并未被跟踪，恰恰是"未跟踪"才触发
            #  ② 第二条建议**必然失败**（git rm --cached 对未跟踪文件 128）
            #  ③ 失败会反噬 —— 用户看到 "did not match" 最自然的解读是
            #     "所以它没进库，不用管" → 忽略警告 → 文件照常被
            #     detect_changes() 拾取推上远端。**这正是 #108 要防的事故。**
            #
            # 所以按成因分三种措辞：只有真被跟踪时才给 git rm --cached。
            _tracked = _audit_guard_tracked(bad)
            if _tracked:
                print(f"  ⚠️  审计流水 {bad} 已被 git 跟踪，会随推送入库。")
                print(f"     建议：git rm --cached {bad}，"
                      f"并把 {os.path.basename(bad)} 加进 .gitignore")
            else:
                print(f"  ⚠️  审计流水 {bad} **未被** .gitignore 屏蔽，"
                      f"会被 detect_changes() 拾取并推上远端。")
                print(f"     建议：把 {os.path.basename(bad)} "
                      f"加进 .gitignore（该文件尚未入库，无需 git rm）")
    # N304（第九十一轮，中危）：`--dry-run` 照写审计流水。
    #
    # 实测一次 --dry-run 写进 8 条记录 —— 而预演**什么都没做**。
    # 后果有两层：
    #   ① 事后排查时，预演的记录与真实推送的记录混在同一条流水里，
    #      无法区分「真的推过」与「只是预演过」。审计的存在理由正是
    #      「那次到底推到哪一步了」（M45），混进去就是污染凭据。
    #   ② 审计流水会**轮转**（保留尾部），预演刷得多就把真实记录挤掉。
    #
    # 与 --dry-run 的其它约定一致：预演不写状态、不改配置、不推远端，
    # 那它也不该写"发生过什么"。
    #
    # N333（第一百〇三轮，中危）：早退**必须放在守卫之后**。
    # 我上一轮把它放在了守卫**之前** —— 于是预演拿不到守卫提示，
    # 与它自己上面那段注释逐字相反（那段写"放在守卫之后"）。
    #
    # 而 `--dry-run` 的用途就是"推之前先看看会发生什么"：
    # 审计文件会不会被一起推上去，正是该在预演阶段知道的事。
    # 真实推送时警告仍会出现，但那时推送已经发生（守卫只提示、不阻断）。
    if _dry_this_run():
        return None
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "run_id": _run_id(),
           "cmd": " ".join(sys.argv[1:])[:200]}
    rec.update(event or {})
    return audit_append(rec)


def audit_path():
    """审计流水路径：与状态文件同目录。

    跟着 STATE_PATH 走而不是另设常量 —— 两者都必须是「本仓库专属」，
    分开定义迟早会在某次改动里分家，然后出现「审计的是 A 仓库、
    操作的是 B 仓库」。
    """
    return os.path.join(os.path.dirname(STATE_PATH) or ".", "push-audit.jsonl")


def _audit_guard_tracked(rel):
    """审计流水是否**已被 git 跟踪**（N320）。

    只有真被跟踪时，`git rm --cached` 才是有效建议 —— 对未跟踪文件
    它必然 `fatal: pathspec did not match`（退出码 128），而用户看到
    这个失败最自然的解读是"所以它没进库，不用管"，于是忽略警告。

    单独抽出来是因为 `_audit_guard()` 的返回值**丢失了成因**：
    三种成因都返回同一个 rel，调用方无从区分。
    """
    try:
        out = git("ls-files", "-z", "--", rel)
    except BaseException:
        return None                 # 探测失败：成因不明，按未跟踪处理
    return bool((out or "").strip())


def _self_artifact_guard():
    """M99（A101）：脚本**自己造出来的**运行时文件会不会被推上远端。

    返回 [(说明, 相对路径), ...]，没问题返回 []。

    _audit_guard() 只管审计流水，而实测（--state 指向工作区内，全新仓库）
    被推上去的是**三个**：

        .state.json        ← 基线：全仓库文件名清单 + sha
        .state.json.lock   ← 锁文件（0 字节，纯属噪声）
        push-audit.jsonl   ← 运行历史：完整命令行原文（含 -m 全文）、
                              时刻、仓库、分支、PR 号

    前两个此前**完全没有守卫** —— 第三个至少有 _audit_guard()（且因
    "文件不存在"豁免，第一次仍然静默）。所以三个文件全程零提示地上远端，
    而脚本自己在 _audit_guard() 的注释里写着：

        "不能指望用户自己配 .gitignore：这是脚本自己造出来的文件，
         就得由脚本自己负责。"

    判据与 _audit_guard() 同口径（在仓库内、不在 .git/ 下、未被 ignore），
    但**不要求文件已存在** —— 理由见上面 M99 的注释。
    """
    try:
        root = git("rev-parse", "--show-toplevel")
    except BaseException:
        return []
    if not root:
        return []
    root = os.path.realpath(root)
    out = []
    # 前三件是脚本**本轮就会写出来**的，所以不要求文件已存在（理由见上面
    # M99 注释）。第四件不同：新版本早已不再往仓库根写状态文件，它只在
    # **老用户升级后残留**时才是问题 —— 不存在就根本不会有它，要求存在。
    for label, path, must_exist in (
            ("状态文件", STATE_PATH, False),
            ("锁文件", (STATE_PATH or "") + ".lock", False),
            ("审计流水", audit_path(), False),
            # 旧版默认把状态文件写在**仓库根**。N316 之后新位置是
            # <root>/.git/push-sync.json，但旧文件还留在原地，而且是未跟踪
            # 的 —— detect_changes() 照样会把它推上去。
            # 实测（裸跑，仓库根留有一份旧 .push-sync.json）：
            # 它既不在 STATE_PATH 上、也不在审计守卫的视野里，全程零提示地上
            # 远端 —— 这是**默认配置**下唯一的一条泄露路径。
            ("旧版遗留状态文件", os.path.join(root, ".push-sync.json"), True)):
        if not path:
            continue
        if must_exist and not os.path.exists(path):
            continue
        try:
            ap = os.path.realpath(path)
        except BaseException:
            continue
        try:
            if os.path.commonpath([ap, root]) != root:
                continue                        # 在仓库外，无所谓
        except ValueError:
            continue                            # 不同盘符（Windows）
        rel = os.path.relpath(ap, root)
        _parts = rel.split(os.sep)
        if ".git" in _parts[:-1]:
            continue                            # git 从不跟踪 .git/ 内的东西
        try:
            if rel in gitignored_set([rel]):
                continue                        # 已被 ignore → 安全
        except BaseException:
            pass                                # 探测失败 → 按有问题报
        out.append((label, rel))

    # N450（第一百四十七轮，中危）：上面那份是**写死的四元组**，而脚本
    # 造文件的实际方式不止这四种 —— 于是守卫报的是"按清单推演可能发生
    # 的问题"，实际发生的那个不在清单里。实测同一次运行内对照：
    #
    #   守卫报出 : .state.json / .state.json.lock / push-audit.jsonl
    #   实际推送 : .state.json / .state.json.corrupt / a.txt.base
    #              / a.txt.remote
    #   漏报     : .corrupt 备份、.base/.remote 副产物
    #
    # 更值得注意的一处对照：守卫报的三项里 .lock 与 push-audit.jsonl
    # **此刻并不存在**（前三件故意不要求存在），而真实存在的那两个
    # 反而一个都没报。
    #
    # 根因是"固定清单 vs 按规则生成的文件名"：.corrupt 带序号后缀、
    # .remote/.base 由**业务文件名**生成（名字无法枚举）。
    # 所以补一层**按规则**的扫描，两者合起来才是完整视野。
    try:
        for label, path in _artifact_paths_by_rule(root):
            try:
                ap = os.path.realpath(path)
            except BaseException:
                continue
            try:
                if os.path.commonpath([ap, root]) != root:
                    continue
            except ValueError:
                continue
            rel = os.path.relpath(ap, root)
            _parts = rel.split(os.sep)
            if ".git" in _parts[:-1]:
                continue
            try:
                if rel in gitignored_set([rel]):
                    continue
            except BaseException:
                pass
            if all(rel != r for _, r in out):
                out.append((label, rel))
    except BaseException:
        pass
    return out


def _artifact_paths_by_rule(root):
    """N450：按**规则**生成脚本自造文件的候选路径（固定清单枚举不完）。

    两类名字无法写死：
      · 损坏备份 `<STATE_PATH>.corrupt[.N]` —— N413 之后带序号，名字不定
      · 冲突副产物 `<业务名>.remote` / `<业务名>.base` —— 由业务文件名
        生成，事前无从枚举（M67 之前还写在仓库内、与源文件同级）
    """
    out = []

    def _add(label, path):
        if path and os.path.exists(path) and not os.path.isdir(path):
            out.append((label, path))

    # ① 损坏备份：带序号后缀，用 glob 而不是拼死一个名字
    try:
        _sp = STATE_PATH or ""
        if _sp:
            _d = os.path.dirname(_sp) or "."
            _base = os.path.basename(_sp) + ".corrupt"
            if os.path.isdir(_d):
                for _n in sorted(os.listdir(_d)):
                    if _n == _base or _n.startswith(_base + "."):
                        _add("损坏备份", os.path.join(_d, _n))
    except BaseException:
        pass

    # ② 冲突副产物目录（N71/M79 之后在 <root>/.git/ 下，天然安全；
    #    但旧布局/自定义位置可能落在工作区内 —— 落在里面就得报）
    try:
        _cd = CONFLICT_DIR if "CONFLICT_DIR" in globals() else None
        if _cd and os.path.isdir(_cd):
            # N467：去掉 [:50] 截断 —— 被截断的永远是字母序靠后的那批。
            for _n in sorted(os.listdir(_cd)):
                _add("冲突副产物", os.path.join(_cd, _n))
    except BaseException:
        pass

    # ③ 旧编码的 .remote / .base：与源文件**同级**，写在工作区内。
    #
    # N466（第一百五十三轮，中危）：原写法 `os.listdir(root)` **非递归**
    #    → 子目录一个都不扫。而本注释自己写的就是「与源文件**同级**」——
    #    源文件在子目录时，落点就在子目录（本仓库的 审核报告/ 测试/
    #    检测工具/ 正是这种形态）。注释描述一般情形，代码只覆盖"源文件
    #    恰好在顶层"这一个特例。实测 sub/b.txt.remote：守卫不报、
    #    --clean-conflicts 不管（它只认 CONFLICT_DIR）、detect_changes
    #    照收 → 远端明文副本零提示进推送清单，且子目录这一支是**静默**的。
    # N467（低危）：`[:200]` 截断 —— 被截断的永远是字母序靠后的那批，
    #    与「哪个文件会被漏」无关，纯看名字。
    # N468（低危）：只按后缀认 → 用户自己的 data.base / notes.remote 被
    #    说成「脚本自己产生的」，还建议加进 .gitignore —— 照做会把它
    #    永久排除出版本控制（再也推不上去，git status 里也看不见）。
    #    而 N450 的注释自己就写过「rel.remote 恰好是真实文件」这个风险。
    #
    # 三条一并解决：改用 `git ls-files --others --exclude-standard`：
    #   · 全仓递归（N466），由 git 自己走，开销远小于 os.walk；
    #   · 不限条数（N467）；
    #   · `--others` = **未跟踪**：脚本造的副产物从未 `git add` 过，而
    #     用户自己的 .base / .remote 业务文件在仓库里是**已跟踪**的
    #     （能推上远端就说明它入库了）。用「在不在索引里」把两类分开，
    #     不再凭后缀一律判给用户（N468）；
    #   · `--exclude-standard` = 已被 .gitignore 屏蔽的不报：它们本来就
    #     不会被 detect_changes 拾取，报出来是噪音（且本节的建议正是
    #     "加进 .gitignore"，屏蔽之后就该闭嘴）。
    #
    # 走 `_git_run` 而不是 `git()`：后者在非 git 目录里会 print 一行
    # 「! git ls-files … 退出码 128」，而本函数挂在 audit() 上，那行
    # 警告会混进每次推送（--mine 的纯数据流契约也会被它破坏）。
    try:
        _rc, _uo, _ue = _git_run("ls-files", "--others",
                                 "--exclude-standard", "-z")
        if _rc == 0:
            for _rel in (_uo or "").split("\0"):
                if _rel.endswith(".remote") or _rel.endswith(".base"):
                    _add("冲突副产物", os.path.join(root, _rel))
    except BaseException:
        pass
    return out


def _audit_guard():
    """审计流水**不能入库**。返回问题说明，无问题返回 None。

    两个理由：
      · 它是本机运行历史，推上去污染仓库（且暴露操作习惯）
      · 每次运行都追加，于是每次推送都有 diff —— 它自己会变成
        「永远推不完的待推送项」

    默认位置在 <root>/.git/ 下，git 不跟踪 .git/ 内的文件，天然安全。
    但 STATE_PATH 若被 --state 指向工作区内（旧版默认就在仓库根），
    审计文件会跟着落在工作区 → 被 git add → 进基线 → 被推送。
    实测确实发生过（一次 migrate 前产生的 push-audit.jsonl 进了索引）。

    不能指望用户自己配 .gitignore：这是脚本自己造出来的文件，
    就得由脚本自己负责。检测到被跟踪就明确告知解决办法。
    """
    p = audit_path()
    # 用 git 自己的 toplevel，不用 ROOT 常量：
    # ROOT 在 resolve_config 之前是编译期默认值（指向另一个仓库），
    # 拿它判断会得到「审计文件在仓库外」的错误结论，防护形同虚设。
    try:
        root = git("rev-parse", "--show-toplevel")
        ap = os.path.realpath(p)
    except (OSError, SystemExit, Exception):
        return None
    if not root:
        return None
    root = os.path.realpath(root)
    try:
        if os.path.commonpath([ap, root]) != root:
            return None                    # 在仓库外，无所谓
    except ValueError:
        return None                        # 不同盘符（Windows）
    rel = os.path.relpath(ap, root)
    # N103：`git ls-files --error-unmatch` 对**未跟踪**文件退出码为 1，
    # 而 git() 的 fatal 处理会把 stderr 直接打出来 → **每次运行**都打印
    # 一行看着像报错的东西。用 -z + 判空代替：不产生 stderr，语义相同。
    try:
        out = git("ls-files", "-z", "--", rel)
    except BaseException:
        out = None
    if (out or "").strip():
        return rel                          # 已被跟踪 → 已经在库里了

    # N303（第九十轮，中危）：上面这条**只覆盖"已被跟踪"**，
    # 而"未跟踪"恰恰是文件刚产生时的状态 —— 也正是 detect_changes()
    # 拾取并推上远端的那条路。
    #
    # 于是这是唯一一处「探测不到 = 判定安全」的方向性错误，
    # 而且反得恰到好处：它只在伤害**已经发生之后**才响。
    # 实测（--state 指向工作区内）：三个文件（状态文件 / 锁文件 /
    # 审计流水）被推上远端，全程零提示。推上去的是完整命令行原文
    #（含 -m 提交说明全文）、运行时刻、仓库名、分支、PR 号、
    # 以及全仓库文件名清单。
    #
    # 正确判据：**被 ignore 才算安全**。未跟踪但没被 ignore 的文件，
    # 就是 detect_changes() 下一次要推的东西。
    #
    # 与脚本自己在同类场景的一贯取向一致（gitignored_set / N76、
    # local_state_map 的 fatal=True、_their_files_by_tree 的
    # fail-closed）—— 全是"拿不到就当有问题"。
    # N303 的两个豁免（否则会把**根本不可能入库**的情形也报成问题，
    # 长期红灯等于没有红灯）：
    #
    # ① 路径在 .git/ 下 → git 从不跟踪 .git/ 内的任何东西，天然安全。
    #    这是**默认位置**（STATE_PATH 在 <root>/.git/ 下），
    #    不豁免的话每次运行都误报。
    # ② 文件根本不存在 → 没有东西会被推上去。
    #    （`--state` 指向工作区但还没产生审计时就是这个状态。）
    # N321（第九十七轮，低危）：原判据用**子串** ——
    #     `".git" + os.sep in rel`
    # 分不清「路径分量等于 .git」与「路径里出现 ".git/" 这个子串」。
    # 实测（真实 git 仓库、文件未跟踪）：
    #     STATE 在 <root>/a.git/   下 → 豁免（不报警）  ← 错
    #     STATE 在 <root>/sub/.git/ 下 → 豁免（不报警）  ← 对
    # 同一判据一半对一半错：
    #   · sub/.git/ 豁免正确（git 从不跟踪任何 .git 目录）
    #   · a.git/    豁免错误（git 正常跟踪它 —— `git add -n` 会把它加进去）
    #
    # 与 N212（`_url_matches` 用子串判主机）、N294（`--repo` 裸名）同一
    # 形态：都是"子串 vs 结构化解析"。
    #
    # 改成**按路径分量判**：任意一层**目录**分量等于 ".git" 才豁免。
    # 注意只看目录段（`_parts[:-1]`）—— 文件本身叫 .git 不算。
    _parts = rel.split(os.sep)
    if ".git" in _parts[:-1]:
        return None
    # M99（A101）：删掉了「文件不存在 → 跳过」这条豁免。
    #
    # 它豁免掉的恰恰是**最先受害的那一次运行**：
    #   · 守卫在 audit() 首次写记录时跑，那时文件还没产生
    #   · 文件在本轮后半段被创建
    #   · 同一轮里 detect_changes() 把它拾取、推上远端
    # 于是"不存在"这个豁免，正好让**产生并推送的那一次**静默通过，
    # 警告从第 2 次运行才出现 —— 而那时东西已经在远端了。
    #
    # 实测（--state 指向工作区内，全新仓库）：
    #   第 1 次运行：零提示，三个文件上远端（含完整命令行原文）
    #   第 2 次运行：才出现「未被 .gitignore 屏蔽」的提示
    #
    # 判据本就不需要文件存在：脚本自己知道它**会**把这三个文件写到哪里，
    # 按脚本一贯的 fail-closed 取向（见上面 N303 注释），不存在也应报。

    # 判"是否被 ignore"复用 gitignored_set —— 与 detect_changes 同一口径。
    # 不能用 `git("check-ignore", "-z", "--", rel)`：
    #   · `-z` 只在带 `--stdin` 时有效（不带会 fatal 退出 128）
    #   · 不带 -z 时命中与否靠退出码 0/1，而 git() 在非 0 时会打印一行
    #     告警 —— 于是"未被屏蔽"这个**正常**情况会每次运行都刷一行，
    #     正是 N103 踩过的坑。
    try:
        _ignored = gitignored_set([rel])
    except BaseException:
        # 探测失败 → 按有问题报（fail-closed）。多报一句提示，
        # 远好过静默把运行历史推上远端。
        return rel
    if rel not in _ignored:
        return rel                          # 未跟踪且未被 ignore → 有风险
    return None                             # 已被 ignore → 安全


def _sanitize_rec(rec):
    """把审计记录里的不可编码字符换成占位符（N148）。

    surrogate 来自 sys.argv（surrogateescape 解码）与异常消息。
    直接 encode("utf-8") 会抛 UnicodeEncodeError —— 而审计在推送**之后**
    写，崩溃时用户刚看到"推送成功"。

    宁可记录里少几个字符，也不能让主流程崩。
    """
    out = {}
    for k, v in (rec or {}).items():
        # 判据是"能不能 encode"，不是"能不能 dumps" ——
        # json.dumps 对 surrogate 不报错，报错发生在后面的 .encode()，
        # 只测 dumps 等于没测（第一版就是这么漏的）。
        #
        # N148②：**键**也要过同一道。上面这段只清洗了值，而
        # json.dumps 会把 surrogate 键原样保留、.encode() 再抛
        # UnicodeEncodeError —— 实测 `{"bad\udcffkey": "v"}` 仍会崩，
        # 且崩在 audit_append 里（except 只兜 OSError，接不住 ValueError）。
        # 键来自 event 字典，眼下都是字面量，但调用点是 `rec.update(event)`：
        # 任何一处把外部字符串当键传进来就会踩到（与本函数要防的
        # sys.argv surrogate 是同一个来源）。
        try:
            json.dumps(k, ensure_ascii=False).encode("utf-8")
            _k = k
        except (UnicodeEncodeError, UnicodeDecodeError, ValueError,
                TypeError):
            _k = _safe_text(k)
        # N606（第二百轮，低危）：清洗会**丢信息** —— `_safe_text` 把
        # surrogate 一律换成 `?`，于是两个不同的坏键清洗后同名：
        #   输入 {'a\udcffb': 1, 'a\udcfeb': 2} → 输出 {'a?b': 2}
        # 第一条被第二条**静默覆盖**，键数 2 → 1，写入侧不报错。
        # 眼下审计的键都是字面量（kind / err / branch），所以影响有限；
        # 但上面那段注释自己论证过「任何一处把外部字符串当键传进来就会
        # 踩到」，同一条论证下的碰撞此前没人处理。
        # 只在**真的撞上**时才加序号：正常路径的键名一个字都不改。
        if _k in out:
            _i = 2
            while f"{_k}#{_i}" in out:
                _i += 1
            _k = f"{_k}#{_i}"
        out[_k] = _sanitize_val(v)
    return out


def _sanitize_val(v, _depth=0):
    """清洗审计记录里的**单个值**，容器**保持结构**（N605）。

    这里原来是 `out[_k] = _safe_text(v)` —— `_safe_text` 对 dict / list
    走 `str(v)`，于是 `passed`（`[{"guard":…,"level":…,"action":…}]`）
    整个变成 Python repr **字符串**，且是双重转义（`\\udcff` 六个字符）。

    写入侧看不出问题：`json.dumps` 收字符串、`audit_append` 返回 True，
    R37-R40 认为修好了。**唯一查看器 `show_audit` 一读就崩** —— 它的
    `for x in passed` 在 passed 是字符串时变成**逐字符迭代**，
    `x.get(...)` 立刻 `AttributeError: 'str' object has no attribute 'get'`。

    也就是说 R37-R40 把问题从「写入时崩」搬到了「查看时崩」。而写入时崩
    发生在推送之后（用户刚看到成功），查看时崩发生在**事后排查**时 ——
    审计存在的理由正是事后排查（`show_audit` docstring 原话：「排查
    『上周那次到底推上去没有』只能靠后者」）。

    修法：递归下探，只在**叶子**上替换占位符，容器类型一律保持原样。
    写入侧与查看器侧对同一份数据的类型约定由这一处统一定义。
    """
    try:
        # 判据是"能不能 encode"，不是"能不能 dumps"（与键侧同源）。
        json.dumps(v, ensure_ascii=False).encode("utf-8")
        return v
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError, TypeError):
        pass
    # 深度上限：防御畸形/循环引用结构，超过就把这层整体降级，
    # 不再下探（与 _safe_text 的兜底同口径）。
    if _depth < 6 and isinstance(v, dict):
        _o = {}
        for _k2, _v2 in v.items():
            try:
                json.dumps(_k2, ensure_ascii=False).encode("utf-8")
                _kk = _k2
            except (UnicodeEncodeError, UnicodeDecodeError, ValueError,
                    TypeError):
                _kk = _safe_text(_k2)
            _o[_kk] = _sanitize_val(_v2, _depth + 1)
        return _o
    if _depth < 6 and isinstance(v, (list, tuple)):
        return [_sanitize_val(_x, _depth + 1) for _x in v]
    return _safe_text(v)


def _safe_text(v):
    """把任意值转成可安全写入 UTF-8 的文本。"""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    t = str(v)
    try:
        t.encode("utf-8")
        return t
    except UnicodeEncodeError:
        return t.encode("utf-8", "replace").decode("utf-8")


def audit_append(rec, path=None):
    """追加一条审计记录。失败只提示一次，绝不抛给调用方。

    用 O_APPEND 单次 write：JSONL 的一行通常 <2KB，远小于 PIPE_BUF，
    Linux 上 O_APPEND 的小写入是原子的 —— 不需要额外加锁
    （且流水线在 StateLock 内写，本就已串行）。
    """
    global _AUDIT_WARNED
    p = path or audit_path()
    try:
        # N148：sys.argv 在 Python 里以 **surrogateescape** 解码 ——
        # 用户点名一个含非 UTF-8 字节的文件名，sys.argv 就带 surrogate。
        # 而这里 json.dumps(ensure_ascii=False) 之后 .encode("utf-8")
        # 会抛 UnicodeEncodeError，且只 except OSError 接不住它。
        # 后果：一次正常的推送，只因文件名编码特殊，就在**收尾时**崩溃
        #（审计在推送之后写），用户看到推送成功却撞上 traceback。
        #
        # 审计是记录手段，绝不能反过来让主流程崩掉 —— 这里做两级兜底：
        # 先确保 rec 可序列化，再确保写出不抛。
        line = json.dumps(_sanitize_rec(rec), ensure_ascii=False,
                          separators=(",", ":"))
        # N137①：原先 open(p,"a") 创建时权限由 umask 决定（实测 0644），
        # **写完才 chmod** —— 中间留一个"刚创建时可读"的窗口。
        # 脚本自己在 _write_conflict_artifact 里为同类问题写过结论：
        # 「事后 chmod 又留一个短暂可读的窗口，os.open 建时给 mode
        #   才是原子的」。审计流水与冲突副产物同为敏感记录，不该两套标准。
        # 首次创建走 os.open（mode 0600）；已存在时直接追加（权限不变）。
        _first = not os.path.exists(p)
        if _first:
            _fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(_fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(_fd)
        else:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        # N117：审计流水记录的是"哪台机器、什么时候、对哪个仓库做了什么"，
        # 信息量不比状态文件低（含仓库名、操作、分支、PR 号），
        # 却沿用默认 umask → 实测 0644，同机其他用户可读。
        # 与状态文件 0600、冲突目录 0700 的口径不一致。首次创建时收紧一次。
        _harden_once(p, 0o600, "audit_journal")
        # N104：audit_rotate 定义了却**从未被调用**（全脚本 0 个调用点），
        # 于是 4MB 上限形同虚设，审计流水只增不减。
        # 而设计注释自述"每天上百次运行，一年几万行"，正是靠轮转兜住的。
        # 写在**追加成功之后**：轮转失败不该影响本次记录，
        # 且只在偶尔超限时才做实际 IO。
        try:
            audit_rotate(p)
        except BaseException:
            pass          # 轮转失败绝不影响主流程
        return True
    # N148②：只兜 OSError 是不够的 —— UnicodeEncodeError 是 **ValueError**
    # 的子类，与 OSError 无继承关系；json.dumps 对不可序列化值抛的
    # TypeError 同理。而本函数的 docstring 承诺的是"绝不抛给调用方"。
    #
    # 最坏的一处后果在失败收尾链（push_fail）：audit() 抛异常会让紧邻的
    # 下一行 `_cleanup_failed_branch` **被跳过** → 远端留下僵尸分支。
    # 而那段代码的注释写的正是"失败必须留下凭据" —— 审计本身成了
    # 清理失败的原因，与它存在的目的相反。
    #
    # save_state 早已是 `except (TypeError, ValueError, RecursionError)`，
    # 这里补 ValueError 即与之同口径（TypeError 不会到这：_sanitize_rec
    # 已在值这一层兜住）。
    #
    # N605（A160，中危）：**RecursionError 漏了** —— 本注释自称"与
    # save_state 同口径"，而那个口径里就有 RecursionError。实测梯度：
    #   n=50 / n=300 层嵌套 → 不抛；n=3000 → RecursionError
    # 路径：`json.dumps(v)`（内层）的捕获列表不含 RecursionError →
    # 传播到外层 `json.dumps` → 这里接不住 → 抛出。
    # 后果正是上面第 3 段写的那一条：audit() 一抛，`_cleanup_failed_branch`
    # 被跳过 → 远端留下僵尸分支；同时违反 `audit_append` 自己的 docstring
    # 「绝不抛给调用方」。
    except (OSError, ValueError, RecursionError) as e:
        if not _AUDIT_WARNED:
            _AUDIT_WARNED = True
            print(f"  ! 审计流水写入失败（不影响本次操作）：{e}")
        return False


def audit_rotate(path=None, max_bytes=AUDIT_MAX_BYTES):
    """超限则保留尾部。返回是否轮转过。

    保留**尾部**而不是头部：最近的运行才是排查时要看的。
    从后往前找换行边界，避免切出半行 JSON。
    """
    p = path or audit_path()
    tmp = p + ".tmp"          # M58：提到 try 外，except 里才能清它
    try:
        if not os.path.isfile(p) or os.path.getsize(p) <= max_bytes:
            return False
        with open(p, "rb") as f:
            f.seek(-max_bytes, os.SEEK_END)
            tail = f.read()
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1:]          # 丢掉可能残缺的第一行
        # N137②：os.replace 会**沿用 tmp 的权限** —— 而 tmp 由
        # open("wb") 创建，权限是 umask 的 0644。于是每次轮转
        # 都把权限打回 0644，且 _harden_once 因"已收紧过"不再收紧。
        # 收紧发生在 replace **之前**（与 save_state 同一条经验）。
        _fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(_fd, tail)
        finally:
            os.close(_fd)
        os.replace(tmp, p)
        return True
    except OSError:
        # M58（A66 实证）：这里原来只 `return False`，tmp **留在磁盘上**。
        # 而它有名字（`p + ".tmp"`）且在审计文件旁边 —— 下次轮转会直接
        # 覆盖它，所以残留不报错、不长大、**不会自己暴露**。
        #
        # 值得修的是它的**内容**：tmp 里是审计流水的一个**前缀切片**
        # （只保留尾部前的那次读取结果），权限 0600，但落在工作目录里
        # 等于留了一份日志副本在审计文件之外 —— 审计文件的轮转/权限
        # 管理都管不到它。
        #
        # 仍然"轮转失败无所谓"（返回值不变）：清 tmp 是收拾现场，
        # 不是把失败转成成功。
        try:
            if os.path.isfile(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        return False                      # 轮转失败无所谓，下次再试


def audit_read(path=None, limit=None):
    """读取审计记录，返回 list[dict]。坏行跳过（不因一条坏记录废掉全部）。"""
    p = path or audit_path()
    if not os.path.isfile(p):
        return []
    out = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue               # 半行/损坏，跳过
    except OSError:
        return []
    if limit:
        return out[-limit:]
    return out



_KIND_LABEL = {
    "guard_pass": "放行",
    "guard_block": "拦下",
    "push_ok": "推送成功",
    "push_fail": "推送失败",
    "push_cancel": "取消",
    # M162：与「取消」分开 —— 这条是**脚本主动拦下**（不是用户按 N）。
    "push_blocked": "拦下（未推送）",
    "merge_ok": "合并成功",
    "merge_block": "合并拦截",
    # M168（A153）：N488 新增了这个 kind（WARN 不再被记成 guard_pass），
    # 而 `_KIND_LABEL` 与 `show_audit` 都没跟 —— 于是它显示成原始字符串
    # "guard_warn"，且明细查不到（判据 13 报的就是这一条）。
    "guard_warn": "警告放行",
}


def show_audit(limit=20):
    """展示最近的运行记录。

    为什么要有这个入口：审计写进去了却没人看，等于没写。
    --status 看的是「现在的状态」，--audit 看的是「发生过什么」——
    排查「上周那次到底推上去没有」只能靠后者。
    """
    p = audit_path()
    recs = audit_read(limit=limit)
    if not recs:
        print(f"\n  没有运行记录（{p}）。")
        print("  审计从本次版本开始记录，之前的运行没有留下流水。")
        return
    print(f"\n最近 {len(recs)} 条运行记录（{p}）：")
    print("  " + "-" * 68)
    for r in recs:
        kind = r.get("kind") or "?"
        label = _KIND_LABEL.get(kind, kind)
        ts = (r.get("ts") or "")[:19].replace("T", " ")
        cmd = (r.get("cmd") or "")[:34]
        head = f"  {ts}  [{label}]"
        detail = ""
        if kind == "guard_block" or kind == "guard_warn":
            # M45：`level` 写进了流水（BLOCK / WARN 等），原先只渲染
            # guard 名 —— 事后看不出这次拦得到底有多硬。
            _verb = "拦下" if kind == "guard_block" else "警告放行"
            detail = f"被 {r.get('guard')} {_verb}"
            if r.get("level"):
                detail += f"（level={r.get('level')}）"
            # M168（A153，中危）：补 `reason` / `detail` 的渲染。
            #
            # 写入侧的注释把审计立为事后复盘「**为什么**这次没推上去」的
            # 唯一凭据，而文件名与字节数（N487 补的）全在这两个字段里。
            # 只写进流水、查看器不渲染 —— 判据 11 会直接判
            # 「记了但查不到，等于没记」。
            # M168 若不补这一半，就只是把同样的缺陷搬到新字段上。
            _why = r.get("reason") or r.get("detail") or ""
            if _why:
                detail += "：" + str(_why).replace("\n", " ")[:120]
        elif kind == "guard_pass":
            # N605（A157，中）：`passed` 必须按**容器**校验后再迭代。
            #
            # 写入侧曾经把整个容器降级成 Python repr 字符串（`_safe_text`
            # 对 list/dict 走 `str(v)`），于是 `for x in passed` 变成
            # **逐字符迭代**，`x.get(...)` 立刻 AttributeError。
            #
            # 写入侧已在 `_sanitize_val` 里改成递归清洗（结构保持），但
            # **旧流水文件里已经躺着字符串形态的记录** —— 那一半改不回来，
            # 而 `--audit` 读的正是那些历史记录。所以查看器这一侧也要挡：
            # 不是容器就当没有明细（宁可少显示一行，也不能一查就崩）。
            _passed = r.get("passed")
            passed = _passed if isinstance(_passed, (list, tuple)) else []
            # M45：`level` 也写进了流水（每个 guard 的级别），
            # 原先只渲染 guard 名 —— 「被拦下」那行看不出级别。
            detail = "经 " + "→".join(
                f"{x.get('guard')}({x.get('level')})" if x.get("level")
                else str(x.get("guard")) for x in passed)
            if not isinstance(_passed, (list, tuple)) and _passed:
                # 上面那句在非容器时不会执行到（空序列 join 出空串），
                # 这里补一句：否则用户看到「经 」后面空空的，
                # 会以为记录丢了，而真实原因是明细字段形态不对。
                detail = "经 （明细字段已损坏，非列表，无法渲染）"
        elif kind == "push_ok":
            # N164：N140/N141/N142 只改了**写入侧**，
            # 这里（唯一的查看器）没有对应渲染 —— 于是
            # "同时被改动的文件" / direct 成功 / 删除了哪些，
            # 三项产出全都写进了流水却**看不到**。
            # 记了但查不到，等于没记。
            _nd = len(r.get("deleted") or [])
            detail = f"{r.get('branch')} {str(r.get('commit'))[:8]} " \
                     f"({r.get('files')} 个文件)"
            if r.get("direct"):
                detail += " [direct 直推主干]"
            if _nd:
                _tag = "待生效删除" if r.get("workflow") == "pr" else "已删除"
                detail += f"，{_tag} {_nd} 个："
                detail += "、".join((r.get("deleted") or [])[:3])
                if _nd > 3:
                    detail += "…"
        elif kind == "push_fail":
            # M45（A50）：`cleaned` 是这一族里最实在的一条。
            # 写入处（push_fail）的注释原话是「失败必须留下凭据：
            # 远端可能已经建了分支/blob……事后要能回答『那次到底推到
            # 哪一步了』」—— 而 `cleaned`（分支是否已清理）**正是**
            # 回答这个问题的字段，却是唯一在查看器里读不到的一个。
            # 实证：推送失败只显示 "HTTPError: 502"，没有清理状态。
            detail = f"{r.get('err')}"
            if "cleaned" in r:
                detail += ("，分支已清理" if r.get("cleaned")
                           else "，分支**未**清理（远端可能残留）")
        elif kind == "push_cancel":
            # 判据 13（A51 后新增）：写进流水的 kind 必须有对应分支，
            # 否则明细查不到。push_cancel 记录了 branch —— 正是"那次
            # 到底推到哪一步"的关键（分支建了没有、清理了没有）。
            detail = f"{r.get('branch') or '-'}"
            if "cleaned" in r:
                detail += ("，分支已清理" if r.get("cleaned")
                           else "，分支**未**清理")
        elif kind == "push_blocked":
            # M162：判据 13 的同族要求 —— 新 kind 必须在唯一查看器里有分支，
            # 否则「记了但查不到，等于没记」（N164 那族）。
            # `guard` 才是这里的关键：它回答「被谁拦下」，而 branch 只回答
            # "推到哪一步"。取消那一条没有 guard。
            detail = f"被 {r.get('guard') or '-'} 拦下"
            if r.get("branch"):
                detail += f"（{r.get('branch')}）"
        elif kind == "merge_ok":
            # M45：`method`（squash/merge/rebase）与 `status` 都写进了
            # 流水，这里原先只渲染 PR 号 —— 事后无法判断那次是怎么合的。
            detail = f"PR #{r.get('pr')}"
            if r.get("method"):
                detail += f" [{r.get('method')}]"
            if r.get("status"):
                detail += f" {r.get('status')}"
        elif kind == "merge_block":
            # N140 修好的 files（改前恒为 None 的 overlap）在这里渲染。
            _hit = r.get("files") or []
            detail = f"PR #{r.get('pr')} {r.get('verdict')}"
            # M45：`why` 写进了流水（截断上限 / 取不到主干改动列表），
            # 当场 print 打完整原因，事后查只剩 verdict: "unknown" ——
            # 而排查时恰恰只剩流水。「当场看得到，事后查不到」。
            if r.get("why"):
                detail += f"（{r.get('why')}）"
            if _hit:
                detail += "，同时改动：" + "、".join(_hit[:3])
                if len(_hit) > 3:
                    detail += f"（共 {len(_hit)} 个）"
        print(f"{head}")
        print(f"     {cmd}")
        if detail:
            print(f"     {detail}")
    print("  " + "-" * 68)


def _parse_headers(path):
    """读 curl dump 出的响应头，取限流相关字段。

    返回 dict（可能为空）。解析失败一律静默返回空 —— 解析限流头只是
    **改善提示**，不该因为格式变化就让请求失败。
    """
    info = {}
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k in ("x-ratelimit-remaining", "x-ratelimit-reset",
                         "retry-after", "x-ratelimit-used", "x-ratelimit-limit"):
                    info[k] = v
    except OSError:
        pass
    return info


def _rate_limit_hint(info):
    """把限流字段转成**能照着做**的提示。

    GitHub 的 Retry-After / X-RateLimit-Reset 都给了确切时间，
    却只提示「等几分钟」—— 用户既不知道等多久，也不知道是不是自己
    刚那次操作（比如预览了 200 个文件）把额度打光的。
    """
    if not info:
        return ""
    parts = []
    used, limit = info.get("x-ratelimit-used"), info.get("x-ratelimit-limit")
    if used and limit:
        parts.append(f"已用 {used}/{limit} 次")
    rem = info.get("x-ratelimit-remaining")
    if rem == "0":
        reset = info.get("x-ratelimit-reset")
        when = ""
        if reset and reset.isdigit():
            try:
                secs = int(reset) - time.time()
                if secs > 0:
                    m, sec = divmod(int(secs), 60)
                    when = f"约 {m} 分 {sec} 秒后" if m else f"约 {sec} 秒后"
            except (ValueError, OverflowError):
                when = ""
        parts.append("额度已用尽" + (f"，{when}重置" if when else ""))
    ra = info.get("retry-after")
    if ra:
        parts.append(f"服务端要求等待 {ra} 秒")
    if parts:
        parts.append("减少预览文件数（--yes 推进）或稍后重跑可缓解")
    return "；".join(parts)



def _sleep_before_retry(code, attempt, hdr_info):
    """重试前的等待。raw 与 JSON 两个分支**共用这一个**。

    分成两处写迟早会漂移 —— 上一次正是这么来的：raw 分支只有固定
    2**attempt，不采纳 Retry-After。而 raw 恰恰只用于拉大文件，
    是最容易撞 429 的路径，等于硬撞限流。

    Retry-After 是服务端给的**确切**等待时间，固定 2/4/8 秒远低于它的
    建议；但也不可无限信任，上限 60 秒兜住异常值。
    """
    wait = 2 ** attempt
    # N377：403 次级限流同样带 Retry-After，与 429 一样采纳。
    if code in ("429", "403") and hdr_info:
        ra = hdr_info.get("retry-after")
        if ra and str(ra).isdigit():
            wait = max(wait, min(int(ra), 60))
    time.sleep(wait)


def _maybe_print_rate_limit(hdr_info, code):
    """限流提示。与 _sleep_before_retry 同理：两个分支共用，不留第二份。

    只在 JSON 分支打印时，**用 raw 拉大文件把额度打光**的场景看不到
    「已用 X/Y 次，约 N 分 M 秒后重置」—— 而这正是这条提示存在的意义
    （预览 200 个文件打光额度，却只提示「等几分钟」）。

    N425：这段原本写在 `global _RATE_LIMITED` 那段**之后**，于是它不是
    `__doc__` 而是一个**死字符串字面量**（Python 只认第一条为文档）。
    后果与 N425 同源：它把本函数的取舍写得完整又专业，看上去就像还在
    生效；而任何"文档 vs 实现"类判据取的是 `ast.get_docstring()` ——
    **只看真 docstring**，死字面量里承诺的东西一条都扫不到。
    """
    # M91（A96）：额度**已用尽**时置位，让批量循环能提前中止。
    #
    # 单次请求的行为本来是对的（退避采纳 Retry-After、提示算出重置时间），
    # 问题在于 × 文件数：脚本**明知额度已尽、明知还要等 60 秒**，
    # 却对剩下 834 个文件各做一遍重试。用户看到的就是"卡住"。
    global _RATE_LIMITED
    if (isinstance(hdr_info, dict)
            and hdr_info.get("x-ratelimit-remaining") == "0"
            and code in ("403", "429")):
        _RATE_LIMITED = True

    if not hdr_info or code not in ("403", "429"):
        return
    hint = _rate_limit_hint(hdr_info)
    if hint:
        print(f"  ! 限流：{hint}")


def _retryable(method, path):
    """这个请求在 5xx / 429 时能否安全重试。

    判据是**幂等性**：服务端可能已经处理完毕、只是响应丢了，此时重发
    非幂等写会造成两类问题
      · PATCH /git/refs：第二次撞 422 not fast-forward，而调用方那句
        「出现并发提交，已中止」会把自己的重试**误报成他人抢先提交**，
        用户据此去排查根本不存在的并发
      · POST /git/commits、/git/trees、/pulls：重复建对象、重复消耗限流额度
    无法区分「没处理」和「处理了但响应丢了」，所以宁可不重试 ——
    失败是安全的（用户重跑即可），误报却会误导排查方向。

    例外：POST /git/blobs 是**内容寻址**的，重发得到同一个 sha，天然幂等，
    且大文件最容易撞 5xx，允许重试收益明显。
    """
    if method == "GET":
        return True
    if method == "POST" and path.startswith("/git/blobs"):
        return True
    return False


# GitHub 错误码 → 可操作的下一步。
# 只给出「原文」是不够的：用户看到 not fast-forward 只会往权限/设置上猜，
# 而正确的动作是 --pull。401/403/404/422 是最常撞的四条。
# M110（A107）：409 的映射**不是**只用于 PR 合并 —— `_raise_api_error`
# 对任何请求都按状态码取这一张表。而 GET /git/refs/heads/main 在
# **空仓库**（刚在网页上建好、还没任何提交）上返回的就是 409：
#     {"message": "Git Repository is empty."}
# 于是首跑拿到的是「PR 合并冲突 / 点 Update branch」这套建议，
# 与真实情况（远端还没有任何一个提交）完全无关。
# 判据按**响应体**里的原文特征分，不动其它 409 的走向。
_EMPTY_REPO_409 = ("远端仓库还是**空的**（一个提交都没有）。\n"
                   "        GitHub 的空仓库上 GET 一个 ref 会返回 409，本脚本无法\n"
                   "        从一个不存在的 main 上取基点。\n"
                   "        先在网页上创建初始文件（勾 Add a README），或本地\n"
                   "        `git push -u origin main` 推一次初始提交，再回来跑本命令。")

# M112（A107）：基准分支写死 `BRANCH = "main"` —— 没有开关（无 env、无选项；
# `--branch` 是**任务分支**名，不是基准分支，实测 `--branch master` 之后
# 请求的仍是 /git/refs/heads/main）。于是默认分支为 master/trunk/develop
# 的仓库必然 404，而 404 的提示是「仓库不存在，或 token 没有访问权限」——
# 把人推向查 token，真实原因（基准分支名不对）一个字没提。
# 这里**不加**任何自动探测（多一次请求、且改变行为），只把这条可能性写进提示。
_NO_REF_404 = ("没有这个 ref。仓库不存在 / token 无权限会返回 404，**基准分支\n"
               "        不是 main 也返回 404**：本脚本的基准分支写死为 main\n"
               "        （`--branch` 指定的是**任务分支**，改不到它）。\n"
               "        若该仓库默认分支是 master / develop / trunk，本脚本用不了。")

_HTTP_HINTS = {
    "401": ("token 无效或已过期：检查 GITHUB_TOKEN 环境变量是否设置、"
            "是否复制完整。\n        （GitHub 的 token 只由 A-Z a-z 0-9 和 _ - . 组成）"),
    "403": ("通常是限流或 token 缺权限"
            "（需要 Contents: write / Pull requests: write）。"),
    "404": ("仓库不存在，或 token 没有该仓库的访问权限。"),
    "409": ("冲突。PR 合并时报 409 有两种，处理方式完全不同：\n"
            "        · 内容冲突 → 先 --pull 合入远端改动；\n"
            "        · 分支不够新（仓库开了「必须与主干同步」保护）→ 在网页点\n"
            "          Update branch 即可，不必本地重推。"),
    "422": ("请求被拒绝。若是 not fast-forward：远端已前进，"
            "先跑 --pull 合入远端改动再推。"),
    "429": ("触发限流，稍后重试。"),
    "451": ("仓库因法律原因被封锁，通常需要联系 GitHub 支持。"),
}


def _conflict_dir_for(root):
    """冲突副产物目录：<root>/.git/push-conflicts。

    N71（前半）：原先是**编译期**由 ROOT 算出、从不更新 → 用 --root 推
    另一个仓库时仍写进原来那个仓库旁边。

    M79（A83，P2）：N71 只修了"**跟随** ROOT"，没修"**按仓库隔离**" ——
    它放在 ROOT 的**父目录**，于是同父目录下的所有仓库共用同一个目录：

        ROOT=/tmp/x/projA → CONFLICT_DIR=/tmp/x/.push-conflicts
        ROOT=/tmp/x/projB → CONFLICT_DIR=/tmp/x/.push-conflicts   ★ 同一个

    `_conflict_path("config.json","remote")` 两个仓库算出**同一路径**，
    A 写完后 B 覆盖，A 再读拿到的是 **B 的远端明文**。

    后果两条：
      (1) 人工参照被污染 —— 冲突输出写"请对照 xxx.remote 人工判断"，
          而同名文件被另一仓库覆盖后，对照的是**别的仓库的明文**。
      (2) `--clean-conflicts` 的安全边界跨仓库失效 —— 它的判据是"是否在
          本仓库 state["conflicts"] 里"，别的仓库未解决的副产物**不在**
          其中 → 被判"已解决"而删掉。而它的 docstring 原话是"仍在
          state["conflicts"] 里的文件，其 .base/.remote 是用户正在对照的
          素材，删掉等于毁掉解决冲突的依据"。

    改到 `<ROOT>/.git/` 下，与 STATE_PATH 同策略。
    安全性：git 不跟踪 `sub/.git/` 形态（N321 已实证 `a.git/` 才会被
    跟踪），故不会入库。
    """
    return os.path.join(root or ".", ".git", "push-conflicts")


def _warn_legacy_conflict_dir(new_dir):
    """M79：旧位置（ROOT 的父目录）还有副产物时提示 —— 不自动搬。

    只提示一次（同一进程内），避免每次清理都刷屏。
    """
    if getattr(_warn_legacy_conflict_dir, "_done", False):
        return
    _old = _legacy_conflict_dir()
    if not _old or not os.path.isdir(_old):
        return
    if os.path.abspath(_old) == os.path.abspath(new_dir):
        return
    try:
        _left = os.listdir(_old)
    except OSError:
        _left = []
    # N533 附带：计数原用裸 listdir —— 子目录也算成「副产物」，
    # 而下面给的 `mv old/*` 会把它们整批搬走。与真正清理那份
    # （_list_conflict_artifacts）同口径：只数普通文件。
    _left = [x for x in _left if os.path.isfile(os.path.join(_old, x))]
    if not _left:
        return
    print(f"  ! 旧位置还有 {len(_left)} 个副产物：{_old}")
    # N533（第一百七十五轮，中危）：新目录是**惰性创建**的 —— 全文件
    # 只有 _conflict_path 里一处 makedirs，只在真要写冲突副本时才建。
    # 而「升级后新位置为空是正常的」（M79 注释原话）与「新目录不存在」
    # 是同一件事：刚升级、还没发生过冲突的仓库，新目录**必然不存在** ——
    # 正是这条提示要服务的那个场景。照抄 `mv … {new_dir}/` 必然 rc=1
    #（target is not a directory），用户只看到自己敲错了一个命令。
    # 附带：计数原用裸 listdir，把子目录与无关文件也算成「副产物」，
    # 而下面给的 `mv old/*` 会把它们整批搬走 —— 恰是「不自动搬」
    # 那条理由本身（里面可能装着别的仓库正在对照的素材）。
    print(f"    确认属于本仓库后自行迁移："
          f"mkdir -p {new_dir}/ && mv {_old}/* {new_dir}/")
    print("    （不自动搬：它可能是同父目录下另一个仓库的）")
    _warn_legacy_conflict_dir._done = True


def _legacy_conflict_dir():
    """旧位置（ROOT 的父目录）。仅用于**提示**迁移，不自动搬。

    不自动搬是因为：那个目录里可能装着**别的仓库**正在对照的素材
    （M79 的实证场景），搬进来等于把别仓库的东西搬进本仓库。
    """
    try:
        return os.path.join(os.path.dirname(ROOT) or ".", ".push-conflicts")
    except Exception:
        return None


CONFLICT_DIR = _conflict_dir_for(ROOT)


def _refresh_conflict_dir():
    """让 CONFLICT_DIR 跟着当前 ROOT 走（N71），并按仓库隔离（M79）。"""
    global CONFLICT_DIR
    try:
        CONFLICT_DIR = _conflict_dir_for(ROOT)
    except Exception:
        pass


def _current_branch_name():
    """当前分支名（detached HEAD / 非仓库返回 None）。

    M135（A123）：切分支与真回退走同一条判据，要靠「记录的提交在哪个分支上」
    区分 —— 而"哪个分支"里得先排除**当前分支**，否则真分叉（old 就在当前
    分支上）也会被误认成切分支。
    """
    try:
        rc = subprocess.run(["git", "-C", ROOT, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True,
                            timeout=DEFAULT_GIT_TIMEOUT)
        if rc.returncode != 0:
            return None
        name = rc.stdout.decode("utf-8", "replace").strip()
        return name if name and name != "HEAD" else None
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return None


def _ancestor_rc(a, b):
    """`git merge-base --is-ancestor a b` 的退出码（0=是祖先，1=不是，>1=查不动）。

    N492（A123）：判断「真回退」还是「提交被重写」要**反过来**再问一次
    （old 是不是 cur 的祖先 / cur 是不是 old 的祖先），而 `_local_head_rewound`
    里已经有一次正向调用 —— 抽出来是为了让两次调用**同一份超时与异常口径**，
    否则一侧接 OSError、另一侧漏接，异常会冒成裸 traceback。

    查不动时返回 -1（表示"不知道"），由调用方按 fail-closed 处理：
    不知道是不是回退 → 按回退说，但**不说死**，见调用点的措辞。
    """
    try:
        return subprocess.run(["git", "-C", ROOT, "merge-base",
                               "--is-ancestor", a, b],
                              capture_output=True,
                              timeout=DEFAULT_GIT_TIMEOUT).returncode
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return -1


def _local_head():
    """本地 git HEAD 的简短 sha（没有仓库时返回 None）。

    用来检测「用户在本地做了回退类 git 操作」。

    P0-1 的判据是 synced_commit == 远端 head_sha，含义是
    「本地包含了远端全部改动」。但 git 操作可以**悄悄改变本地文件内容**
    而不动 synced_commit：

        push v3  → synced_commit = 远端 v3
        git reset --hard HEAD~1  → 本地文件回到 v2，synced_commit 仍是 v3
        push     → P0-1 放行（v3 == v3），第一层也放行（远端 v3 == 基线 v3）
                 → 本地 v2 覆盖主干，v3 静默消失（已实测复现）

    git checkout <old> / git stash pop 同理。所以除了记「基于哪个远端版本」，
    还要记「当时本地 HEAD 在哪」，两者一起才能发现本地被回退。

    N292（第九十四轮，高危）：必须**校验返回值是 sha**，不能拿到什么信什么。

    `git rev-parse HEAD` 在 **unborn HEAD**（`git init` 后还没 commit）
    下退出码 128，但**把参数原样回显到 stdout**：

        $ git rev-parse HEAD
        HEAD
        rc=128

    而 `git()` 在非 fatal/check 时**无条件返回 stdout、不看成败** ——
    于是 `_local_head()` 返回字面量 `'HEAD'`。

    后果链（三重静默）：
      ① 第零层防护失效：`_local_head_rewound()` 拿它去做
         `git merge-base --is-ancestor HEAD <cur>`，git 把 HEAD 解析成
         当前 HEAD → 等价于「cur 是 cur 的祖先」→ **rc 恒 0**
         → 恒判"正常前进"。
      ② 那条专门准备的提示因 `old` 非空而**不触发**。
      ③ `validate_state` 只校验是 str、不校验 sha 格式，照常放行。

    **而且永久** —— 除非重建基线。触发路径恰恰是 USAGE 推荐的第一步
    （`git init` 后立刻跑 `--init-baseline`）。

    与 3850 行一带的论证直接矛盾：那里说 local_head "无条件写……
    记下来只会让回退检测**更早生效**"，前提是「_local_head() 一定返回
    sha」。本条实测推翻了这个前提。

    修法：校验 40/64 位十六进制，否则返回 None → 走 `if not old:` 分支
    → 打印 P1-1 提示。fail-open 但**有声**，好过双重静默。
    """
    out = (git("rev-parse", "HEAD") or "").strip()
    if not out:
        return None
    # 不是 sha 就当"取不到"：宁可让回退检测降级为提示，也不能让它
    # 拿着一个假值恒判"正常前进"。
    if len(out) in (40, 64) and all(c in "0123456789abcdefABCDEF" for c in out):
        return out
    return None


_REWIND_NO_BASELINE_WARNED = False

#: N569（第一百八十六轮，中危）：本次判定出的回退**类别**，供 remedy 侧
#: 分流。取值：`"rewound"`（真回退）/ `"switched"`（切分支）/
#: `"rewritten"`（提交被重写或分叉）/ `"unknown"`（查不动，fail-closed）。
#:
#: 为什么单列一个全局而不是让 `_local_head_rewound()` 多返回一个值：
#: 它的签名 `(bool, why)` 有 5 个调用点，改成三元组要一起改；而 remedy
#: 侧（`_rewind_remedy_lines`）被**两个出口**调用，都必须与 `_local_head_rewound`
#: 的判定同源 —— 否则会出现实证①那样：why 已经说「回退」，remedy 却按
#: 「old 在别的分支上」这个**代理判据**给回 `git checkout <别的分支>`。
#:
#: per-run，必须进 `reset_per_run_globals`（判据 5）。
_REWIND_KIND = None

# N372-告知（A122 / M133）：`local_seen` 取不到时搬家检测**整体静默退化**。
# 一次性：detect_moves 在一次运行里会被 pull / 报告等多处调用，
# 每次都打印等于刷屏，而用户只需要知道一次。
_MOVE_DETECT_UNAVAILABLE_WARNED = False


def _is_sha(v):
    """外部返回的 sha 是否可信（40/64 位十六进制）。

    N292 为 `_local_head()` 加了这条校验，注释写得很清楚：「不是 sha 就
    当『取不到』—— 宁可让回退检测降级为提示，也不能让它拿着一个假值
    恒判『正常前进』」。

    N378（第一百二十一轮，中危）：**同一条推理对 `parents` 完全适用，
    但那里一行校验都没有**。实测把远端 ref 的返回改成 7 位短 sha
    `abc1234`，脚本全程无提示，把它原样塞进 POST /git/commits 的 parents
    → 真实 GitHub 回 422。

    两条路径都漏：API 通道 `parents=[base_sha]`；git 通道
    `commit-tree -p base_sha`。git 侧**更隐蔽**：git 能展开短 sha 所以
    **会成功**，于是"看起来正常"，一旦回退到 API 才炸 —— **回退路径把
    一个在 git 侧被容忍的值，送进了不容忍它的 API。**

    两者都是"外部返回的 sha 要被当成事实使用"。一个校验了、一个没有 ——
    不是策略选择，是**漏了一处**。所以抽成这一个函数，两处共用。
    """
    if not isinstance(v, str) or len(v) not in (40, 64):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in v)


def _local_head_quiet(warnings):
    """取本地 HEAD，把 git 的告警收进 warnings 而不是打到 stdout。

    --status --json 下 stdout 必须是**纯 JSON**：git() 在非 0 退出码时
    默认 print 一行告警，那行会落在 JSON 之前使整体不可解析
    （实测 json.loads 报 "Expecting value: line 1 column 3"）。

    信息不丢：告警原文进 warnings，调用方按需展示。
    """
    import contextlib as _cl
    import io as _io
    _buf = _io.StringIO()
    try:
        with _cl.redirect_stdout(_buf):
            _lh = _local_head()
    except BaseException:
        _lh = None
    _noise = _buf.getvalue().strip()
    if _noise:
        try:
            warnings.append(_noise)
        except BaseException:
            pass
    return _lh


def _local_head_rewound(state):
    """本地 HEAD 是否被回退/分叉（相对上次同步时记录的 local_head）。

    判据：记录的 local_head 应该是当前 HEAD 的**祖先**（本地只会前进）。
    若不是 —— 说明用户在本地 reset/checkout 过，本地内容可能已经
    不包含远端的全部改动，此时推送有静默回退风险。

    返回 (是否回退, 说明)。无法判定时返回 (False, None)，不误报。
    """
    global _REWIND_NO_BASELINE_WARNED
    global _REWIND_KIND
    _REWIND_KIND = None
    old = state.get("local_head")
    if not old:
        # P1-1：local_head 是本版新增字段，升级前的状态文件里都没有。
        # 静默跳过意味着**第零层对它们一律不生效**，用户完全不知道自己
        # 少了这道防护 —— 与「读不到仓库伪装成仓库干净」是同一类静默失败。
        if state.get("files") and not _REWIND_NO_BASELINE_WARNED:
            _REWIND_NO_BASELINE_WARNED = True
            print("  ! 状态文件里没有 local_head（本版新增字段），"
                  "本地回退检测暂不生效，将在下次成功同步后自动启用。")
        return False, None                    # 没记过 → 跳过，不猜
    cur = _local_head()
    if not cur:
        # N495（A123，低危）：与上面 `old` 缺失那一支**同形**，却静默放行。
        # 同一函数、同一个"检测不生效"的结论，两条分支处置标准相反 ——
        # 而上面那支的注释逐字写着静默失效是问题。
        # 不改成 fail-closed（拿不到 HEAD 时拦下会误伤 git 异常的环境），
        # 改成**说出来**：让用户知道这道防护本次没跑。
        if state.get("files") and not _REWIND_NO_BASELINE_WARNED:
            _REWIND_NO_BASELINE_WARNED = True
            print("  ! 读不到本地 git HEAD（git 不可用 / 不是 git 仓库 / "
                  "尚无提交），本地回退检测暂不生效。")
        return False, None
    if cur == old:
        return False, None
    # local_head 是 cur 的祖先？是 → 正常前进
    try:
        # N89：原先不接 stderr —— fatal 信息直接漏到用户终端，
        # 而 subprocess 默认继承父进程的 stderr，于是打印出来
        # 与脚本自己的提示混在一起，看不出是谁的输出。
        # 且只接 TimeoutExpired：OSError（git 不在 PATH 等）会冒成裸 traceback。
        #
        # N492：改走 `_ancestor_rc()` —— 下面判断「真回退 vs 提交被重写」
        # 还要**反向**再问一次（`_ancestor_rc(cur, old)`）。两处若各写一份
        # subprocess.run，超时与异常口径迟早分叉：一处接 OSError、另一处
        # 漏接，异常就只在一侧冒成裸 traceback。抽出来才谈得上同源。
        #
        # 查不动时 `_ancestor_rc` 返回 -1，落到下面 `rc > 1` 那支（fail-closed）。
        rc = _ancestor_rc(old, cur)
        if rc < 0:
            raise subprocess.SubprocessError("merge-base 调用失败")
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        _REWIND_KIND = "unknown"
        return True, (f"判断本地提交 {old[:8]} 是否为当前 HEAD 的祖先时 git 超时"
                      f"（>{DEFAULT_GIT_TIMEOUT}s，仓库过大？）。\n"
                      f"   无法确认本地是否被回退，按「已回退」处理（fail-closed）。"
                      f"\n   确认本地没问题的话：python3 push_api.py --init-baseline 重建基线。")
    if rc == 0:
        return False, None                    # 正常前进
    if rc == 1:
        # P2-3：措辞要覆盖**合法切换分支**——git checkout 另一分支同样
        # 让 old 不是 cur 的祖先，语义上这不是「回退」而是「换了工作树」。
        #
        # M135（A123）：光改措辞不够 —— remedy 首条仍是 `git reset --hard`，
        # 而它重置的是**当前分支**。切分支的用户照做会把当前分支指针改到
        # 另一个分支的提交上（实测：main 被改写、原提交只在 reflog 可达）。
        # 所以这里把「切分支」单独认出来，交给 remedy 侧分流（见
        # RewindGuard.check 里的 `_old_head_on_other_branch`）。
        # N569（第一百八十六轮，中危）：**真回退必须先判**。
        #
        # 原先「切分支」用「old 是否在别的分支上」代理「是否切过分支」，
        # 且排在真回退判据**之前**。两者只在简单情形下等价 ——
        # 实测①（真回退 + old 恰在另一个分支上）：
        #     main: C1 → C2 → C3，记录 local_head = C3
        #     git branch feature C3（feature 与 C3 同一个提交）
        #     git reset --hard C2        ← 真回退，本地内容退回 v2
        #   → `branch --contains C3` = ['feature']、当前分支 main
        #   → _others = ['feature'] → 报「切换了分支」★
        #   真相是 reset --hard 造成的**回退**（`--is-ancestor C2 C3` = 0）。
        #
        # 三层后果，第三层最关键：事实描述错；remedy `git checkout feature`
        # 把人带到不相关分支（main 上的回退一个字节没修）；而切分支那一支的
        # remedy 里**没有** `git reflog` / `git reset --hard` —— 真回退唯一
        # 正确的修法恰恰是它。用户拿到的另一条是 `--reset-baseline`，照做等于
        # 把回退后的旧内容声明为正确基线，下次推送就把主干静默回退 ——
        # 这道防护本要防的事故，由它自己的 remedy 送进去。
        #
        # 触发只需「同步之后从那个点建过一个分支」（git branch / checkout -b），
        # 在 --hold 攒改动或并行开分支的仓库里是常态。
        _rc2 = _ancestor_rc(cur, old)
        if _rc2 == 0:
            _REWIND_KIND = "rewound"
            return True, (f"本地 git HEAD 发生变化（回退）："
                          f"上次同步时是 {old[:8]}，现在是 {cur[:8]}"
                          f"（{cur[:8]} 是 {old[:8]} 的祖先 —— 本地内容确实退回去了）")
        _on_other = _old_head_on_other_branch(old)
        if _on_other:
            _cur_b = _current_branch_name()
            # N569 实证②：detached HEAD 下 `_current_branch_name()` 返回
            # None，而 `[b for b in _on_other if b != _cur_b]` 在
            # `_cur_b is None` 时恒等于整个列表 —— 只要 old 在**任何**
            # 分支上就判「切分支」，真回退再也判不出来。
            #
            # detached 不是边缘形态：actions/checkout 默认就是它，而脚本的
            # --json / --mine 明确面向 CI。拿不到分支名就**不能**声称
            # 「现在在 X 上」，落到下面的「提交被重写或分叉」那一支
            # （fail-safe：其 remedy 仍含 reflog + reset --hard）。
            if _cur_b is not None:
                _others = [b for b in _on_other if b != _cur_b]
                if _others:
                    _REWIND_KIND = "switched"
                    return True, (f"本地 git HEAD 发生变化（切换了分支）："
                                  f"上次同步时是 {old[:8]}（现在在 "
                                  f"{'、'.join(_others[:3])} 上），现在是 {cur[:8]}"
                                  f"（当前分支 {_cur_b or '未知'}）")
        # N492（A123，中危）：`git commit --amend` / `rebase` 与真回退走
        # **同一句文案、同一套 remedy**，唯一区别是两个 sha —— 用户无法
        # 判断自己属于哪一种。
        #
        # 实测（真实 git 仓库）：`git commit --amend -m "..."` **只改提交
        # 说明、不改任何文件**，两棵树完全相同（内容一字未变），仍被判成
        # 「回退/分叉」，拿到与真回退逐字相同的文案与 remedy。
        #
        # 两者其实可以分开，判据是**反向的祖先关系**：
        #   · 真回退   —— cur 是 old 的**祖先**（reset --hard 到更早提交）
        #   · amend / rebase / 合并 —— cur 与 old 是**兄弟**，谁都不是谁的祖先
        # 所以再问一次 `--is-ancestor cur old`：
        #   rc2 == 0 → 确实回退（本地内容把远端改动退掉了）
        #   rc2 != 0 → 提交被重写或分叉，内容**未必**丢，风险等级不同
        #
        # 为什么值得分：真回退必须 `reflog + reset --hard`（内容真的没了）；
        # 而 amend/rebase 场景内容还在本地，正确动作只是**让记录跟上**
        #（--reset-baseline，N494 实测它才有效，--init-baseline 无效）。
        # 给 amend 的用户看「reset --hard 首选」会把他的 amend 成果推倒。
        #
        # （`_rc2` 已在上面**提前**判过 —— 见 N569 那段注释：真回退必须先于
        # 切分支判据，否则真回退会被「old 在别的分支上」误判成切分支。）
        _REWIND_KIND = "rewritten"
        return True, (f"本地 git HEAD 发生变化（提交被重写或分叉，"
                      f"**不是**回退）：上次同步时是 {old[:8]}，现在是 {cur[:8]}"
                      f"（两者互不为祖先，常见于 git commit --amend / rebase / "
                      f"合并；本地内容通常仍包含远端全部改动）")
    # P1-2：rc>1（查不到 old 提交、仓库异常）**不能** fail-open。
    #
    # old 被 gc 回收时 git 返回 128。旧版走 `return False, None` 判成
    # 「未回退」直接放行 —— 而「记录里的提交找不到了」恰恰更像本地被动过
    # 的信号（reset/rebase/amend 都会换掉提交），不是「一切正常」的信号。
    # 静默 fail-open 等于在最该拦的时候解除防护。
    _REWIND_KIND = "unknown"
    return True, (f"无法解析上次同步时记录的本地提交 {old[:8]}"
                  f"（git merge-base 退出码 {rc}）：该提交可能已被 gc 回收。"
                  f"\n   这更像本地被动过的信号，按「已回退」处理（fail-closed）。"
                  f"\n   确认本地没问题的话：python3 push_api.py --init-baseline 重建基线。")


def _old_head_on_other_branch(old):
    """M135（A123，中危）：记录的提交是否**在别的分支上还活着**。

    切分支时 `git merge-base --is-ancestor old cur` 返回 1（old 不是当前
    HEAD 的祖先），于是与真回退走同一条判据、同一句 remedy。

    但两者的正确动作**相反**：
      · 切分支 —— 正确动作是「切回记录里的那个分支」，本地内容没丢；
      · 真回退 —— 正确动作才是 reflog + `reset --hard`。

    而 `reset --hard` 重置的是**当前分支**：用户只是切了个分支，照 remedy
    执行会把**当前分支的指针**改到另一个分支的提交上 —— 那些提交从此只
    在 reflog 里可达（A123 §3 实测：main 被改写、C2 不在 main 历史里）。

    返回分支名列表；查不动/拿不到时返回 None（不猜、不误导流）。
    """
    try:
        rc = subprocess.run(["git", "-C", ROOT, "branch", "--contains", old,
                             "--format=%(refname:short)"],
                            capture_output=True,
                            timeout=DEFAULT_GIT_TIMEOUT)
        if rc.returncode != 0:
            return None
        names = [l.strip() for l in rc.stdout.decode("utf-8", "replace")
                 .splitlines() if l.strip()]
        return names or None
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return None


def _refuse_if_rewound(state, action, extra=""):
    """「声明已同步」之前检查本地回退。回退时返回 True（调用方应中止）。

    必须在写 synced_commit / local_head **之前**调用：那两行一写，
    local_head 就被改写成当前 HEAD，回退证据当场抹掉，第零层从此失效。
    这正是 P0 的根因 —— 判据是对的，坏在出口。

    被拦下的用户最需要知道的是：**远端与基线一致时，--pull 不会动你的
    本地文件**。他跑了 --pull 以为同步好了，其实一个字节都没同步。
    """
    rewound, why = _local_head_rewound(state)
    if not rewound:
        return False
    # 置位「本次运行被防护拦下」，顶层据此把退出码从 1 改成 2。
    #
    # 用标志而不是直接 raise GuardBlock：调用方拿到 True 之后还要做收尾
    # （不写 synced_commit / local_head）。改成 raise 会跳过那些收尾，
    # 制造出「拦住了但状态被写坏」的新问题 —— 那比退出码不对严重得多。
    global _BLOCKED_BY_GUARD
    _BLOCKED_BY_GUARD = "rewound_local_head"
    print(f"\n❌ {why}")
    print(f"   这不是「远端有改动要合并」：远端与基线一致，{action} 没有可合并的内容，")
    print("   所以**不会改动你的本地文件**（这正是它刚才什么都没拉下来的原因）。")
    print("   此时声明「已同步」会让本地旧内容把远端静默覆盖掉。")
    print("   请先把本地恢复到回退前的版本：")
    # N568（第一百八十六轮，中危）：原先这里硬编码 `git reflog` +
    # `git reset --hard`，而 A123（M135）已经论证过**切分支时首条不能是
    # reset --hard**（它重置的是当前分支，实测 main 指针被改写到另一个
    # 分支的提交上、原提交只在 reflog 可达）。
    #
    # A123 把分流加在了 `RewindGuard.check`，**没加回这里** —— 于是同一
    # 份输出里事实判断（"切换了分支"）与 remedy（`reset --hard`）自相
    # 矛盾，而用户被拦下后脚本文本里唯一还会提的 `--pull` 恰恰必经这个
    # 出口：他看到新 remedy 之前的第一个动作就是跑 --pull，然后拿到旧的。
    for _ln in _rewind_remedy_lines(state):
        print(_ln)
    if extra:
        print(extra)
    return True


def _rewind_remedy_lines(state):
    """N568：回退/切分支的解除方式 —— `RewindGuard.check` 与
    `_refuse_if_rewound` **两个出口共用**。

    为什么必须抽出来：A123（M135）给 `RewindGuard.check` 加了切分支分流
    （首条改 `git checkout <分支>`），但 `_refuse_if_rewound` 是**第二个
    打印点**，而且是 `--pull` / `--mark-synced` / `--resolve` 的必经之路
    （5 个调用点、4 个子命令）。同一个判据两条出口给出**相反的**首选
    动作，用户按其中一条执行就会撞上 A123 论证过的那次事故。

    返回行列表（不含末尾的 --force-overwrite / --pull 排除说明 ——
    那两行两条出口的措辞本就不同，各自保留）。
    """
    # N569：按 `_local_head_rewound()` 置的 `_REWIND_KIND` 分流，
    # **不再**用「old 是否在别的分支上」这个代理判据 —— 实证①里 why 已经
    # 说「回退」，而代理判据仍把 remedy 引向 `git checkout <别的分支>`。
    if _REWIND_KIND == "switched":
        _others = _old_head_on_other_branch((state or {}).get("local_head") or "")
        _cur_b = _current_branch_name()
        _tgt = [b for b in _others if _cur_b is not None and b != _cur_b]
        if _tgt:
            return [f"     git checkout {_tgt[0]}"
                    f"        # 你切过分支：先切回记录里的这个分支（首选）",
                    f"     python3 push_api.py --reset-baseline"
                    f"  # 确认本分支内容正确、只是记录对不上"]
    # N493（A123）：`reset --hard` 会丢弃工作区**未提交**的改动
    # （实测：未提交的改动照做后没了）。不标注等于让用户拿数据换解套。
    # N494（A123）：`--init-baseline` 实测**无效**，能解套的是
    # `--reset-baseline`（它更新 local_head 且重建完整基线）。
    return ["     git reflog                  # 找到回退前的提交",
            "     git reset --hard <那个提交>  # 把本地恢复到回退前（首选）",
            "       ⚠️ 会丢弃工作区未提交的改动，先 `git stash` 再看。",
            "     python3 push_api.py --reset-baseline"
            "  # 确认本地内容确实含远端全部改动（amend/rebase 场景）"]


def _conflict_path(rel, kind):
    """冲突副产物（.remote / .base）写到**仓库外**。

    写在 `rel + ".remote"`（仓库内）有三个问题，而且第三个最隐蔽：
      · 可能覆盖用户同名业务文件（rel.remote 恰好是真实文件），无备份不可恢复
      · resolve() 按名字删除时同样可能删掉用户的文件
      · **是未跟踪文件**：detect_changes() 用
        `git status --porcelain -z --untracked-files=all` 且无后缀过滤，
        会把它拾取 → 下次推送直接上远端。
        后果不只是污染：冲突内容等于换个文件名重新提交了一遍，
        若冲突文件含敏感信息就是明文泄露。

    路径里的分隔符换成 `__`，避免嵌套目录名与分隔符混淆。
    """
    # N84：把分隔符压平成 `__` 会让**不同文件撞到同一路径**：
    #   a/b.txt      → a__b.txt.remote
    #   a__b.txt     → a__b.txt.remote      ← 同一个文件
    # 仓库里完全可以同时存在这两个文件，后写的覆盖先写的副本。
    #
    # 先转义已存在的 `__`（`__` → `___`），再压平分隔符。
    # 这样字面 `__` 与"分隔符压平出来的 `__`"可区分，且唯一。
    # 编码抽到 `_conflict_basename()`（M68）—— 清理侧需要它的**逆运算**，
    # 而逆运算只能有一份正确定义；编码留在调用方会立刻变成两份实现。
    # 目录 0700、文件 0600：里面存的是**完整的远端明文内容**
    # （_report_conflict_artifacts 自己就是这么描述的），敏感度高于只含
    # 文件名清单的状态文件 —— 而状态文件早已收紧到 0600。明文那个反而
    # 更松（实测 0777）就没有道理了。
    #
    # makedirs 的 mode 只在**新建**时生效，已存在的目录要补一次 chmod；
    # 这里按「不存在才建、建完统一收紧」处理，避免依赖创建时机。
    # L1（A4 / A23 实证）：`os.makedirs` 会抛 `NotADirectoryError:
    # [Errno 20] Not a directory` —— 父路径上有个同名**文件**时。
    # 实测它会崩掉**整个 pull 循环**。
    #
    # 讽刺点：同一份 pull() 里，作者对远端拉取失败做了极精细的降级
    #（failed 列表、"重跑即可"的指引、不让循环中断）；
    # 而唯一会崩的恰恰是写冲突副本这一步 ——
    # 那只是给人工比对用的**辅助产物**。
    #
    # 所以这里必须降级：建不出来就返回 None，由调用方决定。
    # 冲突副本写不了 → 报出来让用户知道，而不是让 pull 整个死掉。
    try:
        if not os.path.isdir(CONFLICT_DIR):
            os.makedirs(CONFLICT_DIR, mode=0o700, exist_ok=True)
        _ok, _got = _harden_mode(CONFLICT_DIR, 0o700)
        _warn_mode("conflict_dir", CONFLICT_DIR, 0o700, _ok, _got)
    except OSError as _e:
        if not getattr(_conflict_path, "_warned", False):
            _conflict_path._warned = True
            print(f"\n  ! 冲突副产物目录不可用（{_e}），本次不写副本；"
                  f"冲突内容只能从远端重新获取。")
        return None
    return os.path.join(CONFLICT_DIR, _conflict_basename(rel, kind))


def _conflict_artifacts_in(files):
    """M92（A97）：待推送清单里有没有**冲突副产物**（装着远端明文）。

    #108 那次事故的机制（A97 逐版核对历史版本实证）：

        副产物目录落在 git 仓库内 → 未被 .gitignore 屏蔽
        → detect_changes() 正常拾取 → 进待推送清单 → 推上远端

    新位置 `<ROOT>/.git/push-conflicts` 已安全（M79 实证 git 看不见），
    但**旧残留不搬**（不自动搬是对的：那个目录可能是同父目录下另一个
    仓库的素材）。而 ROOT 未必是 git toplevel（`resolve_config` 自己写着
    "cwd 未必是仓库根"）—— ROOT 是子目录时，旧位置
    `dirname(ROOT)/.push-conflicts` **就在仓库内**，残留照样被推。

    判据按**路径分量**比对目录名，不用绝对路径前缀。这一条是实测改
    出来的，两版都失败过：

      ① `os.path.join(ROOT, f)` 与副产物目录比 → 命中 0 条。
         原因：`files` 里的路径相对 **git toplevel**，而 ROOT 可能是
         子目录，两者基准不一致，`os.path.join` 算到另一个目录去了。
      ② 按分量判 `"push-conflicts" in parts` → 仍命中 0 条。
         目录名带**前导点**（`.push-conflicts`），字面比不上。

    所以是 `seg.strip(".") == "push-conflicts"`。

    零误报（A97 实测四例）：`.push-conflicts/a.txt.remote` 命中；
    `foo.remote`（用户自己的文件）不命中；`a.txt` 不命中；混合只命中
    副产物。**不能用"以 .remote/.base 结尾"判**：那是用户自己的合法
    文件名形态，误伤会把正常推送拦下。
    """
    out = []
    for f in files or ():
        if not isinstance(f, str):
            continue
        parts = f.replace("\\", "/").split("/")
        for seg in parts[:-1]:            # 只看目录分量，最后一段是文件名
            if seg.strip(".") == "push-conflicts":
                out.append(f)
                break
        else:
            # N557（第一百八十二轮，中危）：上面只认「目录分量」，于是
            # **旧编码残留**（M67 之前写在 `rel + ".remote"`，与源文件
            # 同级）两个形态都不命中：
            #     a.txt.remote      （顶层）      → 不命中
            #     sub/a.txt.remote  （子目录）    → 不命中
            # 而这个被拒掉的形态**正是脚本自己早年的写法**（`_conflict_path`
            # 的 docstring 第一条就写着旧实现）。实测端到端对照：新布局
            # `.push-conflicts/a.txt.remote` 被拦（退出码 2），`sub/a.txt.remote`
            # 径直进了待推送清单 —— 里面装的是远端文件的完整明文。
            #
            # 自造文件守卫第 ③ 段（按后缀扫）本该兜住，**只扫顶层**，
            # 于是子目录那一格两处都不管（N466）。
            #
            # 拒掉后缀判据的理由是怕误伤用户文件（"以 .remote/.base 结尾"
            # 是合法文件名形态）。但精确解不需要猜后缀 —— 用
            # `_conflict_basename`（编码的唯一定义处，M68 之后唯一）对
            # 「本次已知有冲突的路径 × {remote, base}」生成**确切**的
            # 候选文件名，既不碰 `foo.remote` 这类用户文件，又能覆盖
            # 旧编码残留。
            if f in _KNOWN_ARTIFACT_NAMES:
                out.append(f)
    return out


#: N557：本次运行**已知**的副产物文件路径（含 M67 之前的旧编码形态）。
#:
#: 不是模块级常量 —— 它按仓库/按运行变，所以走 per-run 容器（就地清空，
#: 见 reset_per_run_globals）。
_KNOWN_ARTIFACT_NAMES = set()


def _trim_pending(paths):
    """N559：从**登记侧**（`_RUN_PENDING_DELETES`）裁掉一批路径。

    点名推送时执行侧已按 `allowed` 过滤，但登记侧是 `report_moves` 在
    **过滤之前**写进去的 —— 出口重算口径不含 `allowed`，于是被裁掉的
    路径又出现在出口清单里，并与「本次不处理、仍留在远端」同屏矛盾。

    只做裁剪，不做清空：登记侧是并集口径（只增不减），这里裁的是
    **本次明确不处理**的那批，不是"判据更松的那批"。
    """
    global _RUN_PENDING_DELETES
    if not _RUN_PENDING_DELETES:
        return
    _drop = set(paths or ())
    _RUN_PENDING_DELETES = [p for p in _RUN_PENDING_DELETES
                            if p not in _drop]


def register_known_artifact_names(state=None):
    """N557：把「已知冲突路径 × {remote, base}」的**确切**落盘名登记下来。

    `_conflict_artifacts_in` 拒掉后缀判据是对的（"以 .remote 结尾"是用户
    的合法文件名形态，误伤会把正常推送拦下）。但"猜后缀"不是唯一的解法：

    `_conflict_basename(rel, kind)` 是编码的**唯一定义处**（M68 之后），
    对每个已知 `rel` 都能算出**确切**的那个文件名 —— 既不碰 `foo.remote`
    这种用户文件，又能覆盖旧编码残留（`rel + ".remote"`，与源文件同级）。

    两个形态都登记：
      · 旧编码：`<dirname(rel)>/<basename>`  —— 与源文件**同级**
      · 新布局：`<副产物目录>/<basename>`    —— 已被目录分量那一支覆盖，
        这里一并记上只为口径完整（重复无害，out 去重靠 break/continue）
    """
    try:
        rels = [r for r in (state or {}).get("conflicts") or []
                if isinstance(r, str)]
        for rel in rels:
            for kind in ("remote", "base"):
                try:
                    _bn = _conflict_basename(rel, kind)
                except BaseException:
                    continue
                if not _bn:
                    continue
                # 旧编码：与源文件同级（`_conflict_path` 的 docstring
                # 第一条写的就是 M67 之前的实现）：`<rel>.<kind>`。
                #
                # ⚠ 不能用 `_bn` 去拼 `_d` —— `_bn` 是**扁平**名
                #（`sub/a.txt` → `sub%2Fa.txt.remote`，分隔符被转义进
                # 文件名），拼上 dirname 得到的是 `sub/sub%2Fa.txt.remote`，
                # 磁盘上从不存在这个文件，登记等于没登记。旧编码是
                # **原样** rel 加后缀，两条路完全不同。
                _KNOWN_ARTIFACT_NAMES.add(
                    rel.replace("\\", "/") + "." + kind)
                # 新布局：目录分量那一支已覆盖，这里补上只为口径完整。
                _cp = _conflict_path(rel, kind)
                if _cp:
                    try:
                        _KNOWN_ARTIFACT_NAMES.add(
                            os.path.relpath(_cp, ROOT).replace("\\", "/"))
                    except BaseException:
                        pass
    except BaseException:
        # 登记是告知性动作，绝不许它把主流程带崩；登记不出就退回
        # "只认目录分量"的旧行为（宁可漏报，不可误伤）。
        pass


# 分隔符 → 转义码。Windows 上 `\` 也是分隔符，一并转义。
# Linux 上 `\` 是**合法文件名字符**，绝不能当分隔符转义 —— 那会把
# 名字里带反斜杠的文件改写成另一条路径。
_SEP_ESCAPE = {"/": "2F"}
if os.sep != "/":
    _SEP_ESCAPE[os.sep] = "5C"
_CODE_TO_SEP = {v: k for k, v in _SEP_ESCAPE.items()}


def _conflict_basename(rel, kind):
    """副产物**文件名**（不含目录）—— 编码的唯一定义处。

    M68（A75）：这行原本内联在 `_conflict_path` 里，而
    `clean_conflict_artifacts` 要做它的**逆运算**（从文件名反推 rel），
    于是那边按自己的理解另写了一份 —— 还写错了（见
    `_conflict_rel_from_name`）。

    这是最危险的一种组合：**编得对、解得错**。
    写入侧正常落盘，清理侧永远对不上 → 子目录下的副产物**一个都清不掉**，
    而里面是完整的远端明文。用户看到「无需清理」，
    以为清过了，明文原封不动留在磁盘上。

    抽成函数后写入与反推共用同一份定义，反推还能做往返校验。

    ⚠ 编码方案在 M67 之后换成了百分号风格，不再是 `__` 压平 ——
    原因见下面 `M67` 那段注释。旧格式仍能被**反推**（兼容遗留副本），
    但不再产生。
    """
    # ── M67（A75）：`__` 压平**不是单射**，N84 只修了它举的那一对 ──
    #
    # 旧方案 `rel.replace("__","___").replace("/","__")` 的漏洞：
    # 转义的产物 `___` 与「原生 `_` + 压平出来的 `__`」不可区分。
    # 实测（报告举的三例，git 都能正常跟踪）：
    #     ___/a.txt  → ______a.txt
    #     _/__a.txt  → ______a.txt   ← 同一路径 ★
    #     _/_/a.txt  → ______a.txt   ← 同一路径 ★
    #
    # 后果与 N84 描述的一致（后写覆盖先写的副本），但**静默**：
    # 用户拿到 A 的 `.remote` 以为那是 B 的远端内容。
    # 而 `pull()` 的注释明确说副本是给人工比对用的**依据**
    # —— 依据是错的，比没有依据更糟。
    #
    # 换成百分号风格（先转义转义符本身，再转义分隔符）：
    #   %  → %25        / → %2F      （Windows 上 \ → %5C）
    # 这是单射的：所有 `%` 都被转义过，于是输出里每个 `%` 都必然是
    # 转义序列的开头，解码时不存在歧义。
    out = []
    for _ch in rel:
        if _ch == "%":
            out.append("%25")
        elif _ch in _SEP_ESCAPE:
            out.append("%" + _SEP_ESCAPE[_ch])
        else:
            out.append(_ch)
    return "".join(out) + "." + kind


def _conflict_rel_from_name(name):
    """从副产物文件名反推仓库内相对路径；反推不出来返回 None。

    `_conflict_basename` 的逆运算。两个方向缺一个就是 M68。

    逆**不是**简单地把 `__` 换回 `/`。编码是两步：
      1. `__` → `___`    （转义字面双下划线）
      2. `/`  → `__`     （压平分隔符）
    逆必须倒着做，且第 2 步的逆会被第 1 步的产物干扰：
      "a___b" 里的 `___` 是字面 `__`，不是分隔符 ——
      先替换 `__`→`/` 会把它切成 "a/_b"，反推错。
    所以先用哨兵保护 `___`，再还原分隔符，最后还原字面 `__`。

    末尾做**往返校验**：反推出的 rel 重新编码一次必须与原名逐字相同，
    失败就返回 None（调用方按"认不出"跳过）。这一步是刻意的 ——
    `___` 的切分在极端输入下仍有歧义，与其猜一个可能错的 rel 去删文件
    （不可逆），不如不动。与「删除不可逆、宁可漏删」的一贯取向一致。
    """
    for kind in ("base", "remote"):
        if not name.endswith("." + kind):
            continue
        stem = name[: -(len(kind) + 1)]

        # ① 新格式（M67 起）：百分号转义。单次扫描，遇到 `%` 就吃两位。
        #    因为编码时所有 `%` 都被转义过，这里不存在歧义。
        _out, _i, _bad = [], 0, False
        while _i < len(stem):
            if stem[_i] == "%" and _i + 2 < len(stem):
                _code = stem[_i + 1:_i + 3]
                if _code == "25":
                    _out.append("%")
                elif _code in _CODE_TO_SEP:
                    _out.append(_CODE_TO_SEP[_code])
                else:
                    _bad = True
                    break
                _i += 3
                continue
            _out.append(stem[_i])
            _i += 1
        if _bad:
            _new = None
        else:
            _new = "".join(_out)
            if _conflict_basename(_new, kind) != name:
                _new = None

        # ② 旧格式（`__` 压平）：升级前落盘的副本还在磁盘上，
        #    里面同样是远端明文。认不出来它们就永远清不掉。
        _SENT = "\x00"          # 不会出现在路径里的哨兵
        _old = stem.replace("___", _SENT).replace("__", "/").replace(_SENT, "__")
        if _old == stem:
            _old = None         # 没有 `__` 可还原 → 不可能是旧格式
        elif not _safe_rel(_old):
            # 旧解读可能解出病态结果：实测 `"__"` 会被解成 `"/"`，
            # 而 `os.path.exists(ROOT + "/")` **恒为真** ——
            # 消歧会因此选中这个根本不是仓库内路径的答案。
            # 解出来的必须是合法的仓库内相对路径才有资格参与消歧。
            _old = None

        if _new is None:
            return _old
        if _old is None or _old == _new:
            return _new
        # 两种解读都得出了不同结果 —— **这是真歧义**，不是 bug：
        #   新格式下 "sub__a.txt" 就是 rel 含字面双下划线；
        #   旧格式下它表示 sub/a.txt。
        #
        # 用「哪个 rel 在工作区里真实存在」消歧。判不出来时取新解读
        # （本版写入的格式），旧副本因此可能清不掉 —— 但那只会让
        # 明文多留一会儿，方向是保守的：认错了删掉的是**用户正在
        # 对照的副本**，而副本本该等冲突解决后再清。
        try:
            if (os.path.exists(os.path.join(ROOT, _old))
                    and not os.path.exists(os.path.join(ROOT, _new))):
                return _old
        except BaseException:
            pass
        return _new
    return None


def _write_conflict_artifact(rel, kind, data):
    """写冲突副产物，创建即 0600 —— 里面是远端明文，不能沿用默认 umask。

    不用 `open(p, "wb")`：那样文件权限由 umask 决定（常见 0644 甚至 0777），
    事后 chmod 又留一个「刚创建时短暂可读」的窗口。os.open 建时给 mode
    才是原子的。
    """
    p = _conflict_path(rel, kind)
    if p is None:                    # L1：目录建不出来，写不了副本
        return False
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        f = os.fdopen(fd, "wb")          # 成功即接管 fd 的所有权
    except BaseException:
        os.close(fd)
        raise
    with f:
        f.write(data)
    # N318（第九十六轮，中危）：**文件**这一侧此前没有对应的收紧。
    # `os.open(..., O_CREAT|O_TRUNC, 0o600)` 的 mode 只在**新建**时生效；
    # 文件若已存在（旧版本脚本留下的 / 手工创建的 / 从别的机器拷回来的），
    # 写入后权限保持原样。
    #
    # 实测（`a__b.txt.remote` 预先以 0644 存在）：
    #     写入前 0o644 → 写入后 0o644    ← 远端明文仍是 0644
    #     对照：全新文件 0o600 ✅；目录被改成 0777 后写入 → 0o700 ✅
    #
    # 不对称是确定的：目录**每调用一次**收紧一次（`_conflict_path` 里的
    # `_harden_mode(CONFLICT_DIR, 0o700)`），文件一辈子只收紧一次。
    # 而这里存的是**远端明文**，敏感度高于只含文件名清单的状态文件
    # （本函数 docstring 原话）。
    #
    # 可达性如实说明：需要那个副本在写入前就已存在且权限较松。当前版本
    # 自己写的文件都是 0600，所以不是恒现 —— **升级路径上才会遇到**。
    # 定中危是因为"目录有兜底、文件没有"这个缺口本身不需要额外条件。
    _ok, _got = _harden_mode(p, 0o600)
    _warn_mode("conflict_artifact", p, 0o600, _ok, _got)
    return p


def _cleanup_failed_branch(branch, expect_sha=None, commit_sha=None):
    """推送中途失败时，删掉本次新建的分支（补偿式清理）。

    只删**本次新建**的：`--hold` 复用的分支里可能已有用户攒的改动，
    删掉会丢东西 —— 那种分支保留着，用户可以用 --merge / --prune 处理。

    还要挡住两种「删了反而更糟」的情况：

      · **PATCH 已成功**（分支 sha == commit_sha）：分支上已经有本次推送
        的内容，删掉等于回滚一次已完成的推送。这时该提示用户继续
        （--merge / --branch 复用），而不是悄悄抹掉。
      · **分支已被改动**（当前 sha != 创建时的 base）：说明不是我们留下的
        空分支，可能是别人也在用它，删掉会丢别人的东西。

    清理本身**静默失败**：网络也挂了的时候，删分支的错误会掩盖真正
    的失败原因，让用户看到一堆无关报错。
    """
    if not branch:
        return
    # N94：全脚本有 5 处会删除远端 ref，这里原先是唯一**没过保护闸门**
    # 的一处（另外 4 处都收敛到 _refuse_protected_delete）。
    # 它是"补偿式清理"，分支名来自本次运行 —— 正常情况下永远是
    # task/* 前缀，所以从未出事。但 --branch 可以指定任意名字：
    # 若用户 --branch develop 推送中途失败，这里就会删掉 develop。
    # 同一份常量在 prune() 里说"别动它"、在这里照删不误，自相矛盾。
    if branch == BRANCH or branch in PROTECTED_BRANCHES:
        print(f"  ! {branch} 是受保护分支，跳过失败清理（不删除）。"
              f"如需处理请手工确认。")
        return
    try:
        cur = ref_sha(branch)
    except BaseException:
        return
    if cur is None:
        return                                  # 已经没了
    if commit_sha and cur == commit_sha:
        # PATCH 已成功：分支上已有本次推送的内容。
        #
        # 仍然删除，但必须**明说**，不能静默回滚。理由：
        #   · 这是本次新建的分支（复用的分支 branch_created_now 为 None，
        #     上面已直接返回），内容只来自本次推送，工作区里完整保留，
        #     重跑一次即可重推 —— 删掉不丢东西。
        #   · 留着反而更糟：它既没有 PR、也不在本地 tasks 记录里，
        #     会成为只能靠 --prune 偶然发现的孤儿分支。
        # 报告担心的是「静默回滚」，那就把回滚说出来，而不是不删。
        print(f"\n  ! 分支 {branch} 已含本次推送内容，将回滚删除"
              f"（本地改动仍在，重跑即可重推）")
    elif expect_sha and cur != expect_sha:
        # 必须是 elif：PATCH 成功时 cur 是本次 commit，自然不等于主干
        # head_sha（expect_sha）。若写成独立的 if，这条会把「我们自己刚
        # 推上去的内容」误判成「被他人改动」而跳过清理，孤儿分支就回来了。
        print(f"\n  ! 分支 {branch} 已被改动（不是本次创建的空分支），跳过清理。"
              f"  请手动确认：--prune")
        return
    try:
        api("DELETE", _ref_path(branch), retries=1)
        print(f"  · 已清理本次新建的分支 {branch}（推送未完成）")
    except KeyboardInterrupt:
        # 只重抛 KeyboardInterrupt，不能吞：这里是**失败清理**路径，
        # 用户多半刚按过 Ctrl-C，静默 pass 会让第二次 Ctrl-C 失去响应，
        # 看起来像卡死。
        #
        # 但 SystemExit **必须继续吞**——清理本身失败（网络挂了）正是以
        # SystemExit 抛出的（api 走 _raise_api_error）。若一并重抛，
        # 「清理失败」会盖掉真正的失败原因，那正是这段 swallow 想避免的。
        raise
    except BaseException:
        # 吞掉异常是为了不盖掉真正的失败原因 —— 但**不能不吭声**。
        #
        # 实测：建分支成功、后续失败、且 DELETE 也失败时，分支留在远端，
        # 而输出里**完全没有**提到它。用户看到一堆报错，却不知道远端
        # 多了个孤儿分支。孤儿分支正是「清理盲区」要防的东西，
        # 而它恰恰最容易在「连清理都失败」时出现。
        #
        # 不抛、只提示：真正的失败原因已经在上面报过了，这里追加一句
        # 就够了。用 print 而非 SystemExit 是为了不打断错误传播。
        print(f"\n  ⚠️  分支 {branch} 未能清理（清理请求也失败了），"
              f"已留在远端成为孤儿分支。")
        print("     它没有 PR、也不在本地任务记录里，需要手工处理：")
        print("       python3 push_api.py --prune")
        print("     或重跑一次推送（会新建分支，旧的仍需清理）")


def _ref_path(ref):
    """分支名进 API 路径前必须编码。

    分支名允许含 `/`（task/2026...）以及 `?` `#` `%` 空格等字符 ——
    不编码的话 `?` 之后的部分会被当成 query string，请求打到别的
    端点上去，行为完全不可预测（且可能误操作到别的 ref）。

    只有 find_pr() 原本做了 quote，其余几处都是裸拼。统一走这里，
    避免下次新增调用点又漏掉。

    safe="/" 是**必须的**：GitHub 的端点是 `/git/refs/heads/{ref}`，
    `heads/` 之后的**整段**就是 ref 名（task/2026-xxx 里的斜杠是分支名
    的一部分）。第一版写成 safe="" 把 `/` 编成 %2F，结果所有任务分支
    都变成 task%2F2026xxx，请求全部 404 —— 10 套测试当场变红。
    """
    return "/git/refs/heads/" + quote(ref, safe="/")


def _recheck_ref(ref, expect_sha, when):
    """确认 ref 仍停在 expect_sha，否则中止并给出下一步。

    报错必须说清三件事，否则用户只会往权限上猜：
      · 远端前进到了哪（实际值 vs 预期值）
      · 是在哪个阶段发现的
      · 下一步该做什么（--pull），以及改动**没丢**（避免用户慌张重做）
    """
    now = api("GET", _ref_path(ref))["object"]["sha"]
    if now == expect_sha:
        return
    _blocked(
        f"远端 {ref} 已前进到 {now[:8]}（本次基于 {str(expect_sha)[:8]}），"
        f"{when}检测到并发提交，已中止。\n"
        f"  先跑 python3 push_api.py --pull 把远端改动合进本地，再重新推送。\n"
        f"  （你的改动还在本地工作区，没丢。）"
    )


def _token_kind(tok=None):
    """按前缀判断 token 类型。**只用于报错提示**，不做校验、不拦截。

    起因（外部反馈）：使用者反馈「这个脚本只能配 fine-grained token
    （github_pat_），classic 的 ghp_ 得用 `token xxx` 头，否则写操作
    401 且无原因提示」。

    实测 + 官方文档都不支持这个结论：

      · GitHub 官方文档原话："In most cases, you can use
        Authorization: Bearer or Authorization: token to pass a token."
        —— 两种头与 token 类型**无关**，只有 JWT 必须 Bearer。
      · 本机实测：同一个 github_pat_ token，Bearer 与 token 两种头
        都是 200；造一个假的 ghp_ 去打，无论哪种头都是 401
        —— 401 是因为 token 无效，不是因为头格式。

    所以 401 的真实成因是**凭证本身或它的权限/授权状态**，而脚本原来
    那句「token 无效或已过期…只由 A-Z a-z 0-9 和 _ - . 组成」对所有类型
    一刀切，把使用者推向「换 token 类型」这个无效方向。

    这里按前缀分类型，把**各自最常见的成因**说清楚。
    """
    t = tok if tok is not None else TOKEN
    if not t:
        return "unknown"
    if t.startswith("github_pat_"):
        return "fine-grained"
    # N602（A158，中危）：五种前缀原本**共用同一个 classic 分支**，
    # 于是给它的一律是「勾 repo 作用域 / token 列表里点 Authorize」。
    # 而官方文档里这五种是**五样东西**：
    #   · ghp_ / gho_ —— 个人令牌，设置页上有作用域复选框（提示成立）
    #   · ghu_ —— OAuth App 换来的用户令牌，**权限在 App 上配**，
    #     用户侧没有作用域可勾
    #   · ghs_ —— App 安装令牌，服务端签发、**1 小时过期**
    #   · ghr_ —— refresh token，只能拿去换一个新的 ghu_，
    #     用它调 API **必然 401**（既没有作用域，也不在 token 列表里）
    #
    # 给 refresh token 一句「去勾作用域 / 去点 Authorize」，两个动作
    # 对它都不存在 —— 用户会照着去设置页绕一圈。按语义拆开。
    if t.startswith(("ghp_", "gho_")):
        return "classic"
    if t.startswith("ghu_"):
        return "user-access"
    if t.startswith("ghs_"):
        return "app-install"
    if t.startswith("ghr_"):
        return "refresh"
    if re.fullmatch(r"[0-9a-fA-F]{40}", t or ""):
        return "legacy"
    return "other"


#: 按 token 类型给出 401/403 的**具体成因**。
_TOKEN_HINTS = {
    "fine-grained": (
        "fine-grained token（github_pat_）：\n"
        "          · Resource owner 必须是**这个仓库所属**的账号或组织；\n"
        "          · Repository access 要包含本仓库；\n"
        "          · Permissions 需要 Contents: Read and write、"
        "Pull requests: Read and write（Metadata 自动带上）；\n"
        "          · 最长 366 天，过期即 401；组织可能要求管理员批准。"),
    "classic": (
        # M180（A158，中危）：按前缀分类型之后，把**跨类型的头号成因
        # 弄丢了**。真实 401（GitHub 原文 `Bad credentials`）最常见的
        # 成因就是凭证被吊销 / 过期 / 重新生成后旧值失效，而原来这三条
        # 全是配置与授权方向 —— 作用域早就勾了、SSO 早就授权了、组织
        # 也没禁用，用户会在设置页绕一圈，而「重新生成一个」这个真正
        # 该做的动作提示里没有。补在最前面（它最常见）。
        "classic token（ghp_/gho_）：\n"
        "          · **先排除凭证本身失效**：被吊销 / 已过期 / 重新生成后\n"
        "            旧值即刻作废 —— 重新生成一个并更新 GITHUB_TOKEN；\n"
        "          · 需要勾 `repo` 作用域；要推 .github/workflows 还需 "
        "`workflow`；\n"
        "          · 组织仓库可能需要对该组织做一次 SAML SSO 授权"
        "（token 列表里点 Authorize）；\n"
        "          · 组织可整体禁用 classic token 访问其资源。"),
    "user-access": (
        "OAuth App 的用户令牌（ghu_）：权限在**那个 App** 上配置，\n"
        "          本 token 上没有作用域可勾 —— 去 App 的授权页调整，\n"
        "          或让用户重新授权一次；凭证本身失效同样先排除。"),
    "app-install": (
        "App 安装令牌（ghs_）：服务端签发且**仅 1 小时有效**，\n"
        "          过期即 401 —— 重新换取一个再跑；不要手工粘贴它。"),
    "refresh": (
        "刷新令牌（ghr_）：它**不是**用来调 API 的 —— 只能拿去\n"
        "          `POST /login/oauth/access_token` 换一个新的 ghu_。\n"
        "          用 ghr_ 直接请求**必然 401**，改作用域/做 SSO 都不存在；\n"
        "          请把 GITHUB_TOKEN 换成换出来的那个 ghu_。"),
    "other": (
        # N603（A158，低危）：`_token_kind` 的返回值里有 `other`，而
        # `_TOKEN_HINTS` 的键里没有 —— 于是 `other` 落回
        # `_HTTP_HINTS["401"]`，**正是本函数 docstring 点名要消灭的
        # 那句一刀切文案**。实测会命中 `other` 的真实形态：被截断的
        # token、GitHub App 的 JWT、以及粘成了别的服务的密钥。
        "无法按前缀识别的 token（不是 ghp_/gho_/ghu_/ghs_/ghr_/\n"
        "          github_pat_ 任何一种）：多半是**粘错了值** ——\n"
        "          GitHub App 的 JWT、被截断的 token、或别家服务的密钥 "
        "（如 sk-…）。\n"
        "          请到 Settings → Developer settings 重新复制完整的值。"),
    "legacy": ("40 位十六进制的旧式 token：GitHub 已不再签发，"
               "多半已被回收 —— 建议改用 fine-grained token。\n"
               "          若它其实是某个 **commit sha**，说明 "
               "GITHUB_TOKEN 被配成了 sha（常见 copy-paste 错误）。"),
    "unknown": ("GITHUB_TOKEN 未设置。"),
}


def _auth_hint(code, hdr_info=None):
    """401/403 的可操作提示，按 token 类型分。附带一项实测判断。

    403 最容易误判成「凭证错了」：限流、缺权限、仓库不可达都返回 403。
    这里补一条**零成本**的鉴别 —— 打一次 /user：
      · /user 通过 → token 本身有效，问题在权限/作用域/仓库授权
      · /user 不通过 → 才是凭证问题
    失败一律按"不知道"处理，不猜、不拦。
    """
    kind = _token_kind()
    base = _TOKEN_HINTS.get(kind)
    if code == "401":
        # N515（第一百七十轮，中危）：原先 `base or _HTTP_HINTS["401"]`
        # 是**二选一** —— 三大 token 类型下 base 恒非空，于是
        # 「token 无效或已过期、检查是否复制完整」这句**永不出现**。
        #
        # 而 401 最常见的成因恰恰是它。实测（真实 GitHub、假 token）：
        # GitHub 原文 `{"message":"Bad credentials"}` 就打印在提示上一行，
        # 三种类型却各自指向「勾 repo 作用域」/「SSO 授权」/「改用
        # fine-grained」—— 全是**权限类**成因。用户会照着去勾作用域，
        # 而那个 token 根本不存在。
        #
        # 与 docstring 的关系：本函数立论是「把报错指向真实成因」，
        # 而逐类型的权限提示是**整体替换**了旧的凭证提示，矫枉过正
        # —— 凭证成因被整条删掉。改成**叠加**：先给凭证成因（最常见、
        # 且零成本就能自查），再给该类型的权限成因。
        #
        # unknown（未设置）时 base 已经就是成因本身，不叠加。
        if base is None or kind == "unknown":
            return base or _HTTP_HINTS["401"]
        return _HTTP_HINTS["401"] + "\n          若 token 确实有效，再看：\n          " + base
    # 限流 403 **不鉴别**：此时 /user 大概率同样 403，既问不出结论，
    # 又多打一次请求 —— 而脚本自己刚说过"额度已用尽"。识别不出来
    # （hdr_info 拿不到）时才照常试一次。
    if isinstance(hdr_info, dict) and (
            hdr_info.get("x-ratelimit-remaining") == "0"
            or hdr_info.get("retry-after")):
        # N516（第一百七十轮，中危）：这里**已经明确识别**出是限流
        # （正是靠 x-ratelimit-remaining == "0" / Retry-After），却仍返回
        # `base`（= _TOKEN_HINTS，权限/作用域类提示）。
        #
        # 形态与 N377 相反：N377 是"限流被当成权限去鉴别"，这里是
        # "已判出是限流、却给权限的出路"。两条信息还会同时出现在
        # 同一段输出里 —— `_maybe_print_rate_limit` 说"额度已用尽"，
        # 这里说"去勾作用域"，而后者挂在 `→` 后面，看起来更像
        # "可操作的下一步"。
        #
        # 返回限流口径；限流文案里已含重置时间，不再补权限成因。
        return _HTTP_HINTS.get("429") or _HTTP_HINTS.get(code)
    valid = None
    # N416（高危）：重入保护。
    # 下面的 `api("/user")` 与本函数所在的 `api()` 是**同一个函数** ——
    # /user 返回 403（限流时正是如此）会走同一条
    # `api → _raise_api_error → _auth_hint` 链路，无保护则无限递归。
    # 已在探测中就直接放弃这次鉴别（valid 保持 None = "不知道"），
    # 不再发第二个请求 —— 鉴别本身是**锦上添花**，不该为它放大故障。
    global _AUTH_PROBING
    if _AUTH_PROBING:
        return (base or _HTTP_HINTS.get(code)) if code == "403" else None
    if kind != "unknown":
        _AUTH_PROBING = True
        try:
            # /user 是全站端点，必须给绝对 URL —— api() 会把相对 path
            # 拼到 /repos/{owner}/{repo} 后面，实测得到
            # /repos/.../user → 404，反而把判断带偏。
            r = api("GET", "https://api.github.com/user")
            valid = isinstance(r, dict) and bool(r.get("login"))
        except SystemExit as _e:
            # 401 = 凭证本身被拒 → 明确判 False。
            # 403 这里**不判 False**：/user 也可能因限流返回 403，
            # 那与凭证无关，误判会把用户推向"换 token"这个无效方向。
            _m = str(_e)
            valid = False if "HTTP 401" in _m else None
        except BaseException as _e:
            # N639（A135-M145，中危）：`KeyboardInterrupt` 继承自
            # BaseException，所以探测那 0.5–3 秒里按 Ctrl-C 会被吞掉 ——
            # 实测不传播、进程继续跑完并按 403 退出（退出码不是 130）。
            #
            # 与 A103 实测的「Ctrl-C → 130 且零 traceback」形成对照：
            # 主路径是对的，探测这一支被吞。**用户唯一的紧急出口在这个
            # 窗口里失效**。窗口窄（只在 /user 那一次请求期间），但
            # 重抛的成本是零。
            #
            # 注意 finally 不能动 —— N416 的重入保护必须在异常路径也复位，
            # 拿掉会让整个进程此后再也不做鉴别。重抛也会走 finally。
            if isinstance(_e, KeyboardInterrupt):
                raise
            valid = None
        finally:
            # N416：必须 finally —— 异常路径漏复位会让**整个进程此后**
            # 再也不做鉴别（静默降级，比临时放弃更难发现）。
            _AUTH_PROBING = False
    if valid is True:
        # N601（A158，中危）：两条分支的**详细度给反了** —— 这里是
        # 「/user 通过 → 凭证没问题、用户正需要去改权限」的情形，正是
        # 最需要本类型清单的时候，却只给一句通用文案（实测 163 字符、
        # 不含 base）；而 valid is False 那一支（凭证有问题、改权限
        # 无用）反而带完整清单（240 字符）。
        #
        # 附带一处事实错误：`Contents: write` 是 **fine-grained 的
        # 权限名**，classic token 的设置页上只有 `repo` / `workflow`
        # 这类**作用域复选框** —— classic 用户照着去找 "Contents: write"
        # 一个都找不到。而正确写法就在同一函数上方的 `base` 里。
        #
        # 按 kind 分流权限名，并把本类型清单带上。
        # M186（A162，中危）：上面两档只对 fine-grained / classic 成立。
        # `Contents: write` / `Pull requests: write` 是 **fine-grained 的
        # 权限名**；legacy（多半是 commit sha 或已回收的旧 token）与 other
        # （粘错了值：别家密钥、App 的 JWT）的设置页上都**没有**这两项，
        # `repo` 作用域 likewise 也不属于它们 —— 5 类里 4 类拿到的是
        # 别人的清单。这两类给中性口径，具体检查项由下面的 base 带出。
        if kind == "fine-grained":
            _perms = ("通常是权限不足：需要 Contents: write / "
                      "Pull requests: write；\n"
                      "          或该仓库/组织没把这个 token 纳入授权范围。")
        elif kind == "classic":
            _perms = ("通常是权限不足：需要 `repo` 作用域（含 "
                      "`workflow`，若要推 .github/workflows）；\n"
                      "          或该仓库/组织没把这个 token 纳入授权范围。")
        else:
            _perms = ("通常是权限不足（本脚本需要仓库的写权限与 PR 权限）；\n"
                      "          这类 token 的入口随签发方式而异，"
                      "请看下面该类型的具体检查项。")
        return ("token 本身**有效**（/user 通过），所以不是凭证问题 ——\n"
                f"          {_perms}\n"
                f"          当前 token 类型：{kind}"
                + ("\n          该类型的具体检查项：\n          " + base
                   if base else ""))
    if valid is False:
        # N638（A135-M144，中危）：这里**已经确切知道**凭证有问题（/user
        # 也返回 401），却把 `_TOKEN_HINTS`（权限/作用域类）排在最前面，
        # 而真正的结论只有末尾那半句括号。
        #
        # 实测（/user 抛 SystemExit(401)）：
        #     classic token（ghp_/…）：
        #             · 需要勾 `repo` 作用域…
        #             · 组织仓库可能需要对该组织做一次 SAML SSO 授权…
        #             · 组织可整体禁用 classic token 访问其资源。
        #             （/user 也未通过 → 凭证本身有问题）   ★ 结论垫底
        #
        # 三条全是**无效动作**（token 不存在时勾作用域不会有任何变化），
        # 而结论被压成括号里的补充。与 N515（401 侧：凭证成因被整条删掉）
        # 是同一件事的两个出口 —— 那侧已改成"凭证在前、权限降为参考"，
        # 这侧没跟。
        #
        # 同口径：先给凭证成因，权限成因降为参考。
        return (_HTTP_HINTS["401"]
                + "\n          （/user 也未通过 → 凭证本身有问题）"
                + ("\n          若 token 确实有效，再看：\n          " + base
                   if base else ""))
    return "限流、缺权限、或仓库不可达都返回 403。\n          " + (base or "")


def _raise_api_error(method, url, code, body, hdr_info=None):
    """统一的 API 报错出口：GitHub 原文 + 可操作的下一步。

    raw 分支与 JSON 分支**共用这一个出口**。分成两处写迟早会漂移 ——
    上上轮的 P0 正是 raw 分支漏改造成的（引用了不存在的变量）。
    """
    hint = _auth_hint(code, hdr_info) if code in ("401", "403") \
        else _HTTP_HINTS.get(code)
    # M110：同一状态码在不同请求上含义不同，按响应体特征分流。
    # 只在**明确命中**空仓库特征时替换；其余 409 一字不动。
    if code == "409" and body and "Git Repository is empty" in body:
        hint = _EMPTY_REPO_409
    # M112：只在**取 ref** 这条路径上替换（404 也用于取文件内容、取 PR 等，
    # 那些场景与基准分支无关，不能一并改口径）。
    elif code == "404" and "/git/refs/heads/" in url:
        hint = _NO_REF_404
    msg = f"API {method} {url} → HTTP {code}"
    if body:
        msg += f": {body[:400]}"
    if hint:
        msg += f"\n  → {hint}"
    raise SystemExit(msg)



def _curl_once(method, url, payload=None, raw=False, timeout=None,
               auth_cfg=None, hdrf=None):
    """一次 HTTP 往返。返回 (returncode, stdout, stderr, rawfile)。

    transport 层：只管发请求、把原始结果带回来。**不做**重试判断、
    不做报错、不做限流提示 —— 那些全在 api() 里统一处理一份。

    为什么必须抽出来：raw 与 JSON 两个分支原先各自写了一遍
    subprocess.run 与后续处理，≈60 行近似重复。重复代码的问题不是
    「多写几行」，而是**修一处漏一处**：历史上 raw 分支漏改
    _sleep_before_retry（不采纳 Retry-After），而 raw 恰恰只用于拉
    大文件、最容易撞 5xx —— 那次漂移正是这个结构造成的。

    rawfile：raw=True 时是响应体临时文件路径（调用方负责读 + 清理），
    否则为 None。不在这里读出来：非 2xx 时响应体是错误 JSON，
    为它读一次可能很大的文件纯属浪费；且原实现就是这样延迟读的。
    """
    cmd = [
        # -L 跟随重定向：仓库改名 / 迁移后 GitHub 会返回 301，
        # 不加 -L 时 curl 把 301 的响应体原样返回，脚本按状态码
        # 判断会看到 404 —— 报「仓库不存在」，而真实原因是改名了。
        # 排查方向完全跑偏。
        "curl", "-sL", "--max-time", str(timeout), "-X", method, url,
        "--config", auth_cfg,          # token 走这里，不进 argv
        "-H", "Accept: application/vnd.github.raw" if raw
              else "application/vnd.github+json",
        "-H", "Content-Type: application/json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
        "-D", hdrf,                    # 响应头 → 文件
        "-w", "\n%{http_code}",
    ]
    if raw:
        # 原始字节写临时文件：不进 stdout，避免 8bit 数据被 text=True 破坏，
        # 也避免大文件整份驻留内存两次。
        # 状态码仍由 -w 写进 stdout，所以能和非 raw 分支共用重试判定。
        tmp = tempfile.NamedTemporaryFile(prefix="pushapi-", delete=False)
        tmp.close()
        cmd += ["-o", tmp.name]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True)
        except BaseException:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
        return out.returncode, out.stdout, out.stderr, tmp.name

    if payload is not None:
        body = json.dumps(payload)
        # 大 payload 走临时文件：10MB 文件在推送路径上会同时存在
        # base64 串、JSON 串、编码后字节**三份**，峰值内存约 3×。
        # 让 curl 直接读文件（@file），Python 侧只留一份。
        if len(body) > 8 * 1024 * 1024:
            pf = tempfile.NamedTemporaryFile(
                prefix="pushapi-body-", mode="w", delete=False, encoding="utf-8")
            try:
                pf.write(body)
                pf.close()
                cmd += ["--data-binary", "@" + pf.name]
                out = subprocess.run(cmd, capture_output=True, text=True)
            finally:
                try:
                    os.unlink(pf.name)
                except OSError:
                    pass
        else:
            cmd += ["--data-binary", "@-"]
            out = subprocess.run(cmd, input=body, capture_output=True,
                                 text=True)
    else:
        out = subprocess.run(cmd, capture_output=True, text=True)
    return out.returncode, out.stdout, out.stderr, None


def _retry_after_curl(rc, stderr, max_time, method, url, attempt, retries):
    """curl 进程本身失败时：能重试就 False（继续），否则抛 SystemExit。

    返回 True 表示「已决定不再重试，调用方应跳出」。

    超时（28）**一律不重试** —— 服务端可能已生效，重试会重复建对象、
    重复消耗限流额度。

    网络层失败（7 / 52 / 56 …）要过 _retryable()：这类失败与「响应丢了」
    无法区分 —— 服务端可能已经处理完，只是传输中断。对非幂等写重试的
    后果 _retryable() 里写得很清楚：PATCH /git/refs 第二次撞 422，
    而调用方那句「出现并发提交，已中止」会把**自己的重试**误报成他人
    抢先提交，用户据此去排查一个根本不存在的并发。
    """
    if rc == 28:
        raise SystemExit(
            f"{'拉取' if method == 'GET' else '请求'}超时（{max_time}s）{method} {url}\n"
            f"  不重试：服务端可能已生效，重试会重复创建。请确认远端状态后重跑。")
    # 原文里 raw 分支写的是「拉取超时」、JSON 分支写的是「请求超时」。
    # 统一成按 method 选词：GET 是拉取，其余是请求 —— 语义一致且不必
    # 维护两份文案（两份文案必然漂移，且漂了没人看得见）。
    if attempt < retries and _retryable(method, url):
        time.sleep(2 ** attempt)
        return False
    raise SystemExit(f"curl 失败（退出码 {rc}）: {stderr}")


def api(method, path, payload=None, retries=3, timeout=None, raw=False,
        allow_404=False):
    """调 GitHub API。path 以 http 开头时按绝对 URL 用。

    raw=True 时改取原始字节（Accept: application/vnd.github.raw），
    用于拉大文件内容——JSON 形式的 blob 对超过 1MB 的文件不返回 content。
    此时返回 bytes 而非 dict。raw 只用于 GET，幂等，仍按下方策略退避重试。


    显式检查 HTTP 状态码：curl -s 会把 403/404/422 的错误体也原样返回，
    不检查的话调用方只能靠 "sha" in resp 这种启发式猜，排查成本很高。

    重试策略（**区分幂等性**，以 _retryable() 为准，不是这里写的）：
      · 超时（curl 退出码 28）**一律不重试** —— 服务端可能已生效，
        重试会重复建对象、重复消耗限流额度
      · curl 网络层失败（52/56 等）：同样过 _retryable，因为这类失败
        也可能是「服务端已处理、响应传输中断」
      · 5xx / 429：仅当 _retryable() 为真才重试 ——
        实现只对 GET 与 POST /git/blobs（内容寻址、天然幂等）返回 True
      · 429 的等待时间取 Retry-After（若有），不只用 2^n

    ⚠ 上一版 docstring 写的是「5xx：GET / POST 都退避重试」，与实现
    **不一致**：_retryable 从未对非 blob 的 POST 返回 True。
    写错的文档比没文档更糟 —— 维护者会照着它去「修正」实现，
    把安全的部分改得不安全。文档必须跟着实现走。
    """
    require_token()
    url = path if path.startswith("https") else BASE + path
    max_time = str(timeout or DEFAULT_TIMEOUT)
    # 响应头写临时文件：限流信息只在头里，不解析就只能给「等几分钟」
    # 这种模糊提示 —— 用户不知道要等多久，也不知道是不是自己的操作
    # （比如 preview 拉了上百次 blob）把额度打光的。
    #
    # 在循环**外**创建一次：curl -D 每次会覆盖文件，重试时无需重建；
    # 放循环内则每个 continue 分支都要记得删，极易漏（且漏了会积累
    # 一堆临时文件）。统一在 finally 里清理一处即可。
    #
    # N482（A116）：这里走**系统临时目录**（/tmp），而 `_auth_config`
    # 的 cfg 走 /dev/shm —— 两者刻意不同源，原先没有任何注释说明，
    # 看起来像漏了一处硬化。理由是内容不同：
    #   · cfg 装 **PAT 明文**，值得放内存盘（不落磁盘）
    #   · hdr 装**响应头**（限流信息），实测不含 token
    # 而 `raw`（远端文件明文）是 curl 直接写盘的，不经过本函数 ——
    # 那条路径若要也挪进 /dev/shm，是独立的一项改动，不由本编号覆盖。
    hdrf = tempfile.NamedTemporaryFile(prefix="pushapi-hdr-", delete=False)
    hdrf.close()
    try:
      with _auth_config() as auth_cfg:
        for attempt in range(1, retries + 1):
            rc, stdout, stderr, rawfile = _curl_once(
                method, url, payload, raw, max_time, auth_cfg, hdrf.name)
            try:
                if rc != 0:
                    # 抛 SystemExit 表示不重试；返回 False 表示已 sleep 过，继续循环
                    _retry_after_curl(rc, stderr, max_time, method, path,
                                      attempt, retries)
                    continue

                # 状态码由 -w 追加在 stdout 末行
                code = (stdout.rpartition("\n")[2].strip()
                        if stdout else "")
                hdr_info = _parse_headers(hdrf.name)
                _maybe_print_rate_limit(hdr_info, code)

                if code.startswith("2"):
                    if raw:
                        with open(rawfile, "rb") as f:
                            return f.read()
                        # 注意：rawfile 由下面的 finally 清理
                    data = _json_or_die(stdout.rpartition("\n")[0], code)
                    return data
                if allow_404 and code == "404":
                    return None

                body_txt = stdout.rpartition("\n")[0]
                # N377（第一百三十二轮复核对）：GitHub 的**次级**限流
                # 返回 **403**（不是 429），同样带 Retry-After。
                # 而 403 不在重试列表里，于是 `_maybe_print_rate_limit`
                # 打印完「服务端要求等待 N 秒」就**立刻退出，一秒都不等**
                # —— 提示承诺了脚本不会做的事。
                #
                # 只在**确实带 Retry-After** 时才把 403 当可重试：
                # 403 更常见的成因是凭证/权限问题，那种重试无意义且白耗
                # 额度。判据带得上这个头，才说明是限流。
                # 幂等性仍由 `_retryable(method, path)` 把关（非幂等写
                # 不重试 —— 见它的 docstring）。
                _ra403 = (code == "403" and isinstance(hdr_info, dict)
                          and hdr_info.get("retry-after"))
                retryable = ((code in ("500", "502", "503", "504")
                              or code == "429" or _ra403)
                             and _retryable(method, path))
                if retryable and attempt < retries:
                    # 429 时 GitHub 会在 Retry-After 里给出确切等待时间，
                    # 固定 2/4/8 秒远低于它的建议 —— 按建议等，别硬撞。
                    _sleep_before_retry(code, attempt, hdr_info)
                    continue
                # raw 分支没有响应体文本可传（响应写进了临时文件）。
                # 传 None 而不是直接用变量名 —— 用不存在的变量会 NameError，
                # 上上轮的 P0 就是这么来的。
                _raise_api_error(method, url, code,
                                 None if raw else body_txt,
                                 hdr_info)
            finally:
                if rawfile:
                    try:
                        os.unlink(rawfile)
                    except OSError:
                        pass
    finally:
        try:
            os.unlink(hdrf.name)
        except OSError:
            pass


def _json_or_die(body, code):
    """解析响应体，失败时给出带状态码的报错。"""
    try:
        return json.loads(body or "{}")
    except json.JSONDecodeError:
        raise SystemExit(f"响应非 JSON (HTTP {code}): {body[:400]}")


def _git_add_paths(paths):
    """git add 一批路径，用 --pathspec-from-file 而不是 argv 展开。

    `git add -- *paths` 在文件数上千时会触碰 ARG_MAX（典型 2MB），
    触发 E2BIG 让整个推送失败。--pathspec-from-file 从 stdin 读路径，
    没有 argv 长度限制。

    用 -z（`--pathspec-file-nul`）接收 NUL 分隔：路径可能含换行，
    按行分隔会把它拆成两个 pathspec。

    失败时**回退**到逐个 add：批处理模式下单个路径有问题（比如被
    .gitignore 拦下）会让整批失败，而逐个子进程时坏的会被跳过、
    正常文件仍进暂存区。宁可慢一点，不能一个坏路径拖垮整批。

    返回**是否有路径真的进了暂存区**（由调用方判定，不看 git 退出码）：
    实测踩过 —— 75 个路径里 1 个被 .gitignore 屏蔽，`git add` 退出码 1，
    但**其余 74 个照样进了暂存区**。只看退出码会误判成「全失败」，
    只打一行 `!` 又会让调用方以为「全成功」。两者都错。
    """
    if not paths:
        return True
    rc = 0
    # N562（第一百八十四轮，中危）：超时**不能**塞进 rc=1。
    #
    # M100 修的是「括号里的原因对 rc=128 不成立」，而上面这个
    # `except TimeoutExpired: rc = 1` 是**另一个入口**：把超时赋成 1，
    # 于是超时走进「rc=1 ⇒ .gitignore」这条叙事。
    #
    # 决定性对照（250 个真实文件、DEFAULT_GIT_TIMEOUT=0.001）：
    #     打印「批量 git add 退出码 1，逐个重试一遍
    #          （250 个；被 .gitignore 屏蔽的路径会跳过）」
    #     ★ 含「.gitignore」: True   ★ 含「超时」: False
    # 而真被 .gitignore 屏蔽时 git add 的返回码**也是 1** —— rc 相同
    # 走同一条 else 分支，打印逐字相同的一句。用户会去翻 .gitignore，
    # 而真实原因是 git add 超过 120 秒，且 250 个文件无一被屏蔽。
    # 响了却说错原因，比不响更容易把排查带偏。
    _timed_out = False
    if len(paths) < 200:
        # 少量路径没必要走 stdin，直接 argv 更简单也更易诊断。
        # 失败同样要回退：这一支原先 `return` 掉没有回退，
        # 于是 <200 路径时一个被忽略文件就能让整批都进不了暂存区。
        rc, _, _ = _git_run("add", "--", *paths)
        if rc == 124:
            # N562：<200 这一支走 `_git_run`，它内部把超时编成 **rc=124**
            # （git 自身不会返回 124，见 `_git_run`）。不在这里置位的话，
            # 124 会掉进下面 `else` 那条「退出码 {rc} … 被 .gitignore
            # 屏蔽的路径会跳过」的叙事 —— 与 >=200 支**同一个 bug 的
            # 第二个入口**：
            #     实测（3 个路径、mock _git_run → 124）：
            #         打印「批量 git add 退出码 124，逐个重试一遍
            #              （3 个；被 .gitignore 屏蔽的路径会跳过）」
            #         ★ 含「.gitignore」: True   ★ 含「超时」: False
            # 只修 >=200 支会留下一条"报告里的决定性对照测不到"的漏网路径
            # —— 而报告用的正是 250 个文件那一支。
            _timed_out = True
    else:
        try:
            proc = subprocess.run(
                ["git", "-C", ROOT, "add", "--pathspec-from-file=-",
                 "--pathspec-file-nul"],
                input="\0".join(paths), capture_output=True, text=True,
                errors="surrogateescape", timeout=DEFAULT_GIT_TIMEOUT)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            # N562：不复用 rc=1 —— 那是 .gitignore 的返回码。
            _timed_out = True
            rc = -1
    if rc == 0:
        return True
    if _timed_out:
        print(f"  ! 批量 git add **超时**（>{DEFAULT_GIT_TIMEOUT}s，"
              f"仓库过大或磁盘慢），逐个重试一遍（{len(paths)} 个）")
        print("     这与 .gitignore 无关 —— 路径都是真实存在的文件；"
              "逐个重试时每个文件单独计时，不会再整体超时。")
        ok = _git_add_one_by_one(paths)
        return ok
    # 措辞必须中性：rc != 0 **不等于**失败。
    # 路径匹配 .gitignore 但文件已被跟踪时，git 照样 add 成功却返回 1，
    # 此时说「未成功」是错的，会让人以为改动没进暂存区。
    # M100（A102，dogfood 实测）：删除文件时 rc 恒为 **128**，而这句
    # 的括号里写的是「被 .gitignore 屏蔽」—— 那是 rc=1 的解释，
    # 对 128 完全不成立：
    #   rc=1   → The following paths are ignored（.gitignore）
    #   rc=128 → fatal: pathspec 'X' did not match any files（本地已无此文件）
    # 实测（--delete 删一个文件）：每次都打这一行，括号里的原因是错的。
    # 用户会去翻 .gitignore，而真实原因是文件刚被删掉。
    if rc == 128:
        print(f"  ! 批量 git add 退出码 128（pathspec 不匹配，"
              f"通常是这些文件已不在本地，如刚删除的），逐个重试一遍"
              f"（{len(paths)} 个；确实不存在的会跳过）")
    else:
        print(f"  ! 批量 git add 退出码 {rc}，逐个重试一遍"
              f"（{len(paths)} 个；被 .gitignore 屏蔽的路径会跳过）")
    ok = _git_add_one_by_one(paths)
    return ok


def _git_commit_paths(msg, paths, cname, cmail):
    """本地补提交一批路径：≥200 走 `--pathspec-from-file`，与 add 同口径。

    N83（第二十五轮）：这里原先**只有 argv 展开一条路**
    （`commit -q -m msg -- *git_todo`），而同一个文件里的 `_git_add_paths`
    早在 <200 / ≥200 上分了两支。于是"待提交路径上万"时：
      · add 走 stdin，安全；
      · commit 撞 ARG_MAX（典型 2MB，按 100 B/路径约 2 万条）→ OSError
        → _git_run 兜住返回 rc=1 → local_commit_state="commit_failed"
        → 打印「远端已生效，但本地补提交失败」。
    不会崩也不会丢远端内容，但**出错的正是脚本本可以避开的那一步**，
    且报错只说"commit 失败"，看不出是路径太多。

    用 -z（`--pathspec-file-nul`）：路径可能含换行，按行分隔会拆成两个
    pathspec（与 add 同一条理由）。实测 git 2.34 支持这两个选项
    （`git commit -h` 输出含 --pathspec-from-file / --pathspec-file-nul）。

    返回值与 `_git_run` 同形（(rc, stdout, stderr)），调用方不用改判据。

    M178（A157，低，本轮只改注释不改行为）：**上面这段把「不再撞
    ARG_MAX」说成已解决，而它只在路径这一维成立** —— `-m msg` 在两条
    分支上都走 argv（本支的 `commit -q -m msg`、下面 stdin 支同样），
    于是 msg 一超过 Linux 的单参数硬上限 `MAX_ARG_STRLEN`（128 KB），
    两条分支无一幸免：

        130000 B → 正常；131000 B → 正常；131100 B → ★ E2BIG
        longmsg(200KB) + 5 条(argv)      rc=1 ★
        longmsg(200KB) + 250 条(stdin)   rc=1 ★
        longmsg(200KB) + 60000 条(stdin) rc=1 ★

    git 没有 `--message-from-file`，所以这条路当前无解（`commit` 的
    `-F` 读的是另一个语义的模板）。不改行为，只把边界写下来 ——
    否则下一位读到「不会撞 ARG_MAX」就不会再去查 `-m` 这一维
    （与 A128 族同源：注释描述了修复，实现只修了一半）。

    后果本身可诊断（报错是 `git 无法启动：[Errno 7] Argument list too
    long`，能被打出），且不丢远端内容，只是本地补提交失败并如实报
    commit_failed，故定低危。
    """
    if len(paths) < 200:
        # 少量路径走 argv：更简单，也更容易从命令行复现诊断。
        return _git_run("-c", f"user.name={cname}",
                        "-c", f"user.email={cmail}",
                        "commit", "-q", "-m", msg, "--", *paths)
    try:
        proc = subprocess.run(
            ["git", "-C", ROOT,
             "-c", f"user.name={cname}", "-c", f"user.email={cmail}",
             "commit", "-q", "-m", msg,
             "--pathspec-from-file=-", "--pathspec-file-nul"],
            input="\0".join(paths), capture_output=True, text=True,
            errors="surrogateescape", timeout=DEFAULT_GIT_TIMEOUT)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        # 与 _git_run 同口径：超时不裸抛（这里是**收尾**步骤，抛出去会
        # 中断整次推送的收尾，而远端已经生效了）。
        return 124, "", "git commit 超时"
    except OSError as e:
        return 1, "", f"git 无法启动：{e}"


def _git_add_one_by_one(paths):
    """逐个 git add，返回是否至少有一个成功。

    单独成函数是因为 `_git_add_paths` 的两条分支（argv / stdin 批量）
    都要走它，写两遍迟早会漂。
    """
    ok = False
    for p in paths:
        rc, _, err = _git_run("add", "--", p)
        if rc == 0:
            ok = True
        elif "did not match any file" in (err or "") or "ignored" in (err or ""):
            # 被 .gitignore 屏蔽 / 路径不存在：预期内，不刷屏
            continue
    return ok


def safe_input(prompt, default="", kind="confirm"):
    """读一行输入；非交互环境（无 TTY / 管道 / CI）返回 default 而不抛异常。

    `input()` 在 stdin 关闭或耗尽时直接抛 EOFError。脚本里有 9 处交互确认，
    任何一处抛出来都是**裸 traceback 中断整个流程** —— 用户看到的不是
    「请加 --yes」而是一堆栈，且不知道该怎么继续。

    顶层虽然也兜了 EOFError，但那只能把 traceback 换成人话，**流程已经断了**：
    比如 --merge 时先列了 PR 再问选哪个，EOF 一抛，用户连列表都白看了。

    默认值一律取**安全侧**（""，即"否"）：
      · 确认类问题（删除 / 覆盖 / 合并）→ 不确认，宁可什么都不做
      · 提交信息类 → 空串，由调用方回落到默认文案
    绝不能默认"同意"——否则 CI 里所有确认都会被静默放行。

    `kind` 区分**确认型**与**数据录入型**（N311，第九十三轮）：
      kind="confirm"（默认）→ "否"有意义，返回 default 是对的
      kind="entry"        → 录入的是数据（如 PR 号），"否"没有意义。
                            返回 "" 会让调用方走进"用户取消了"的分支，
                            而实际是**根本没拿到输入**。
                            两者文案也必须不同 —— 对录入型说
                            "所有确认按否处理"会让用户以为自己拒绝了什么。
    """
    _is_entry = (kind == "entry")
    # K1（A25 唯一 P1）：全文 `isatty` 出现 0 次 —— 上面这段 docstring
    # 承诺"非交互环境（无 TTY / 管道 / CI）返回 default"，
    # 而实现**只在 EOFError 时**回落 default，从不检测 stdin 是不是 TTY。
    #
    # 管道里有数据时根本不走 EOF 分支：
    #     echo "log" | push_api.py
    #     git diff --name-only | xargs push_api.py
    # 实测：stdin = io.StringIO('y\n')、isatty=False → 返回 'y'
    #      （对照：空 / 纯换行 → EOF → '' 按否 ✓）
    #
    # 后果：删除分支、覆盖远端这类**不可恢复**的操作被静默放行。
    # 这是全部条目里唯一能让"删除/覆盖"在无人确认时被放行的一条。
    #
    # docstring 的原则完全正确（"绝不能默认同意"），**检测没做**。
    try:
        _tty = sys.stdin.isatty()
    except BaseException:
        _tty = False                 # 拿不到 → 按最保守处理
    if not _tty:
        if not getattr(safe_input, "_warned", False):
            safe_input._warned = True
            if _is_entry:
                print("\n  ! 非交互环境（无 TTY / 管道 / CI），"
                      "无法录入内容 —— 请改用命令行参数显式指定。")
            else:
                print("\n  ! 非交互环境（无 TTY / 管道 / CI），"
                      "所有确认按「否」处理。加 --yes 或 -y 可跳过交互。")
        return default
    try:
        return input(prompt)
    except EOFError:
        # 只提示一次：多个确认点连续 EOF 时会刷屏。
        if not getattr(safe_input, "_warned", False):
            safe_input._warned = True
            print("\n  ! 非交互环境（无 TTY / 管道 / CI），"
                  "所有确认按「否」处理。加 --yes 或 -y 可跳过交互。")
        return default


# git 超时。个别 git 操作（大仓库的 hash-object、ls-files）可能挂住，
# 没有超时会让整个推送永久卡死 —— 用户只能 Ctrl-C，而那时远端状态未知。
#: N397（第一百二十九轮）：一次推送从远端净移除多少**行**内容才提示。
#: 只提示不拦下 —— 正常重构也会净删除，硬拦会误伤日常使用；
#: 但被移除的行必须打出来，否则并发覆盖永远是静默的。
#: 0 = 关闭。
REMOVAL_WARN_LINES = int(os.environ.get("PUSH_API_REMOVAL_WARN", "30"))

#: N397 / M97（A100）：一次最多对多少个文件拉远端内容做逐行比对。
#:
#: 每个文件一次 GET /git/blobs，串行、无缓存。脚本自己在 8383 行就为
#: 同一件事立过先例（PREVIEW_CAP = 20），注释原话：
#:   "100 个文件 = 100 次额外请求…… 撞上限流后报 403"
#: 本处原先**没有上限**，实测 60 个文件把推送期 blob 请求从 20 拉到 80。
#: 外推到 835 个文件 = 835 次，正是 A95/M91 记过的限流长尾。
#:
#: 取 20 与 PREVIEW_CAP 一致：真实事故场景是「两个会话改同一个文件」，
#: 一个文件也覆盖得到；而"每个文件都查"换来的边际收益远小于限流风险。
REMOVAL_CHECK_CAP = int(os.environ.get("PUSH_API_REMOVAL_CAP", "20"))

DEFAULT_GIT_TIMEOUT = 120

# git **传输通道**（push / hash-object 批量写对象）的超时。
# A2（A24 降级 P3）：这个值原先硬编码成字面量 180，与上面的 120
# 是两个互不引用的数字 —— 改一处不会同步另一处，
# 且报错文案里还写着第三个 180（"「>180s」"）。
# 实测 21 处引用里 20 处已接常量，只有这一处没有。
# 收成具名常量：值与文案同源。
GIT_TRANSPORT_TIMEOUT = 180
DEFAULT_COMMIT_NAME = "push-api"
DEFAULT_COMMIT_EMAIL = "push-api@localhost"


def git(*args, check=False, fatal=False, timeout=None):
    """跑 git。check=True 时非 0 直接中止；fatal=True 时给**可诊断**的报错。

    默认不中断但**必须告警**：只看 stdout 会让失败完全静默——
    `git add` 部分路径被 .gitignore 拦下时退出码为 1，
    正常文件虽然进了暂存区，调用方却以为全部成功。

    只去掉末尾换行，**不用 strip()**：git status --porcelain 的 X 列是
    「暂存区状态」，未暂存修改输出为 " M a.txt"（开头一个空格）。
    strip() 会把这个空格吃掉，字符串变成 "M a.txt"，按 `item[3:]` 切出来的
    路径就成了 ".txt" —— 最常见的「改完文件没 add」场景会静默推空。

    fatal=True 与 check=True 的区别：check 只抛原始错误信息，fatal 会
    先跑 _git_env_error() 给出**成因与解法**。用于「git 挂了就完全没有
    正确性可言」的读取路径（detect_changes / local_state_map）——
    它们一旦返回空，脚本会把「读不到仓库」伪装成「仓库是干净的」。
    """
    try:
        proc = subprocess.run(["git", "-C", ROOT, *args], capture_output=True,
                              text=True, errors="surrogateescape",
                              timeout=timeout or DEFAULT_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        # 超时也要给可诊断的错误，不能裸抛 TimeoutExpired 堆栈。
        raise SystemExit(_git_env_error(
            f"git {' '.join(_scrub_url(str(a)) for a in args)[:60]} 超时"
            f"（>{timeout or DEFAULT_GIT_TIMEOUT}s）",
            ""))
    except OSError as e:
        # OSError 必须接住：路径数上万时 argv 展开会撞 ARG_MAX
        # （实测 20000 个路径 → OSError: [Errno 7] Argument list too long），
        # 而这里原先只捕获 TimeoutExpired，于是裸 traceback 冒给用户。
        # 转成可诊断的错误而不是修掉具体那处调用 —— 兜底要放在**所有**
        # git 调用的公共出口上，否则下一处 argv 展开还是会漏。
        raise SystemExit(_git_env_error(
            # M57（P1）：args 里可能含内嵌 token 的 origin URL。
            f"git {' '.join(_scrub_url(str(a)) for a in args)[:60]} 无法启动：{e}",
            str(e)))
    if proc.returncode != 0:
        msg = f"git {' '.join(_scrub_url(str(a)) for a in args)} 退出码 {proc.returncode}"
        if proc.stderr.strip():
            msg += f"：{_scrub_url(proc.stderr.strip()[:300])}"
        if fatal:
            raise SystemExit(_git_env_error(msg, proc.stderr or ""))
        if check:
            raise SystemExit(msg)
        print(f"  ! {msg}")
    return proc.stdout.rstrip("\n")


def _commit_identity():
    """本地补提交用谁的身份：**优先用用户自己配的那个**。

    原来硬编码 `user.name=yuanbao / user.email=yuanbao@users.noreply.github.com`，
    后果是别人的仓库里多出一个挂着不相干身份的提交，GitHub 贡献归属也会
    算到那个账号上 —— 而用户在**不改脚本**的前提下无法纠正。

    优先级：
      1. PUSH_API_GIT_NAME / PUSH_API_GIT_EMAIL —— 显式覆盖（CI 用得上）
      2. 仓库里已有的 git config user.name / user.email
      3. 兜底默认值（保证连 git 全局配置都没有时仍能提交）

    值一律校验：控制字符（含换行）会污染 commit 对象；
    仓库级的 .git/config 是仓库自带的、不可信来源，不能照单全收。
    """
    def pick(env_key, git_key, default):
        v = (os.environ.get(env_key) or "").strip()
        if not v:
            # 「没配 user.name」是非 0 退出码，git() 会因此打印告警 ——
            # 但那是**预期内**的常态（全新环境 / CI 容器里很常见），
            # 每次本地补提交都打一条噪声会把真正的问题淹没掉。
            try:
                pr = subprocess.run(["git", "-C", ROOT, "config", "--get", git_key],
                                    capture_output=True, text=True,
                                    errors="surrogateescape",
                                    timeout=DEFAULT_GIT_TIMEOUT)
                v = pr.stdout.strip() if pr.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                v = ""
        if not v or not v.isprintable():
            return default
        return v
    return (pick("PUSH_API_GIT_NAME", "user.name", DEFAULT_COMMIT_NAME),
            pick("PUSH_API_GIT_EMAIL", "user.email", DEFAULT_COMMIT_EMAIL))


def _git_run(*args, timeout=None):
    """跑 git，返回 (退出码, stdout, stderr)。

    git() 只回 stdout，对绝大多数调用方够用 —— 它们读的是路径列表 / sha，
    成功与否从输出本身就能判断。但**本地补提交**不行：

      `git commit` 成功时 stdout 为空，失败时 stdout 也常常为空
      （错误走 stderr）。只看输出无法区分「提交成功」与「pathspec 不
      匹配导致整批未提交」。实测就是栽在这里：75 个文件里 1 个被
      .gitignore 屏蔽 → commit 整体失败、75 个一个都没落，
      而脚本看 stdout 照样打印 ✅。

    所以凡是「必须知道到底成没成」的写操作，走这个而不是 git()。
    """
    try:
        proc = subprocess.run(["git", "-C", ROOT, *args], capture_output=True,
                              text=True, errors="surrogateescape",
                              timeout=timeout or DEFAULT_GIT_TIMEOUT)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(_scrub_url(str(a)) for a in args)[:60]} 超时"
    except OSError as e:
        # 与 git() 同理：argv 展开撞 ARG_MAX 时不能裸抛。
        # 返回非 0 退出码而不是抛异常 —— 调用方（收尾补提交）靠退出码
        # 判定成败，抛出去会中断整次推送的收尾流程，比「本地没提交」
        # 严重得多（远端已经生效了）。
        return 1, "", f"git 无法启动：{e}"


def _staged_any(paths):
    """暂存区里有没有这些路径的改动。返回 (rc, 是否有, stderr)。

    只需要一个布尔值，所以**完全不传 pathspec**：
      · 路径数上万时 argv 展开会撞 ARG_MAX（OSError E2BIG，实测复现）
      · `git diff` 不支持 --pathspec-from-file（实测 git 2.34 直接退出码
        129 打 usage）—— 那个选项是 git add / reset / commit 才有。
        想当然地套用会得到「静默失败」：非预期退出码被当成出错，
        于是收尾补提交被判成失败，而实际上 add 早就成功了。

    改为取全量暂存列表（-z，无 pathspec，不受 ARG_MAX 影响），
    再在 Python 里与 paths 求交。暂存区条目通常远少于仓库文件数，
    代价可忽略，且不依赖任何 git 版本特性。

    返回结构沿用 _git_run 的 (rc, stdout, stderr)，让调用方改动最小。
    """
    if not paths:
        return 0, "", ""
    rc, out, err = _git_run("diff", "--cached", "--name-only", "-z")
    if rc != 0:
        return rc, "", err
    staged = {p for p in out.split("\0") if p}
    hit = staged & set(paths)
    return 0, ("有" if hit else ""), ""


def _report_push_result(target_ref, commit_sha, *, pr=None, url=None,
                        direct=False, hold=False, local_commit_state=None,
                        todo=None, ignored_files=None, msg=None,
                        pure_delete=False, state=None, head_sha=None):
    """推送结果的**唯一窗口**。

    之前同一条信息散在三处，各打各的：
      · 推送前打一次待删除清单（随后被几十行 blob 进度冲到屏幕外）
      · 收尾再打一次（同一个清单印两遍）
      · --status 又有一份
    「同一份逻辑多处、改一漏一」正是这批审查报告反复批评的形态，
    我不该再复制一遍。

    所以收敛成一条规则：
      · **本次已经做完的事**（推到哪、PR、本地补提交）→ 结果窗口
      · **没做、需要你去做的事**（待删除清单）→ 也在这里，一次说清
      · 删除**开启**时不在收尾重复 —— 那属于"即将发生"，
        由推送前的确认环节负责（见 report_pending_deletes 的 _on 分支）

    这样每个场景只出现一次，且都在正确的时机：
      开启 = 推送前警告（让你确认）
      关闭 = 收尾待办（让你手动删）
    """
    print()
    print("=" * 62)
    print(f"  ✅ 推送完成  →  {target_ref}（{commit_sha[:8]}）")
    print("=" * 62)
    if pr:
        print(f"  PR #{pr['number']}: "
              f"{pr.get('html_url') or pr.get('url')}")
    if url:
        print(f"  {url}")
    if direct:
        print(f"  基线已同步（{STATE_PATH}）")

    # ---- 本地补提交：结果由事实驱动，不用 git log -1 冒充成功证据 ----
    if local_commit_state is not None:
        _report_local_commit(local_commit_state, todo or [],
                             ignored_files or [], msg or "",
                             pure_delete=pure_delete)

    # 待删除清单**不在这里打**：它由运行出口统一输出（且只输出一次）。
    # 这里再打一遍就是同一份清单出现两次 —— 见 report_pending_deletes
    # 的 emit 参数说明。
    # ---- 后续动作 ----
    if hold:
        print("  --hold：分支与 PR 已保留，未合并。攒够改动后：")
        print("     python3 push_api.py --merge")
    print("=" * 62)
    print()


def _report_local_commit(state_name, todo, ignored_files, msg,
                         pure_delete=False):
    """本地补提交的结果**由事实驱动**，不用 git log -1 冒充成功证据。

    旧版无条件打印「本地已补提交 <git log -1 的 hash>」—— 那个 hash 是
    当前 HEAD，**与本次提交是否成功毫无关系**：commit 失败时照样显示，
    是一句直接违背事实的陈述。实测就是被它误导的：远端 74 个文件已生效、
    本地 0 个提交，屏幕上却是绿 ✅ + 「已补提交 xxxx」。
    """
    if state_name == "ok":
        print(f"   本地已补提交 {git('log', '-1', '--format=%h')}")
        return
    if state_name == "noop":
        # 不算失败：内容本就与 HEAD 一致（常见于是刚 --pull 完再推）
        print("   · 本地无新内容需要提交（工作区与 HEAD 一致）")
        return
    if state_name == "skipped":
        # N60：这句措辞在**纯删除**场景下是假的。
        # 实测：纯删除推送时 files=[]、todo=[]、git_todo=[] →
        # local_commit_state="skipped" → 打印这句。
        # 但本次**没有推送任何文件**（tree 里只有删除条目），
        # "本次推送的文件"这个集合根本不存在；
        # 真实原因是 todo 为空（无文件内容可提交），与 gitignore 无关。
        #
        # 用户读到「推送的文件都不在 git 跟踪范围内」会以为
        # 有一批文件被跳过了，而实际上一个文件都没有。
        if pure_delete:
            print("   · 本次为纯删除提交（无文件内容改动），无需本地提交；"
                  "远端已删除上述路径。")
        else:
            print("   · 本次推送的文件都不在本地 git 跟踪范围内，未做本地提交")
        return
    # add_failed / commit_failed：远端已生效，本地没有
    skipped = [p for p in todo if p in ignored_files]
    print("\n⚠ 远端已生效，但本地补提交失败：工作区仍是脏的。")
    if skipped:
        print(f"   其中 {len(skipped)} 个被 .gitignore 屏蔽，本地 git 不跟踪："
              f"{skipped[:5]}{' …' if len(skipped) > 5 else ''}")
    print("   后果：下次 detect_changes 仍会列出这批文件，"
          "而基线已按「远端已同步」记录 ——")
    print("   两者不一致会让改动进入「推了但没完全推」的状态。")
    print("   手工补上（被 .gitignore 屏蔽的需 -f）：")
    for p in skipped[:3]:
        # M176：这两处原先是裸路径（`git add -f -- {p}`），而 13 行之外
        # 的 commit 那行已由 N92 加了引号 —— 同一个函数、处理同一个
        # `todo` 集合，转义口径两种。文件名含 `$(…)` 时照抄就执行了。
        print(f"     git add -f -- {_shq(p)}")
    others = [p for p in todo if p not in ignored_files][:3]
    for p in others:
        print(f"     git add -- {_shq(p)}")
    if len(todo) > 6:
        print(f"     … 其余 {len(todo) - 6} 个同上")
    # N92：这条建议的 commit **不带 pathspec**，而脚本自己的实现是带的
    #（`commit -q -m msg -- <git_todo>`，第 6084 行注释写明：
    #  "只提交 todo 这些路径，避免把别人先前 git add 进暂存区的文件
    #   一起卷进来"）。
    # 建议与实现相反 —— 用户照着补救，就把暂存区里别人的东西一起提交了。
    # 补救命令必须和实现同一个口径。
    #
    # M176（续）：N92 只给 path 加了引号，**msg 没有** —— 它被直接塞进
    # `"..."` 里。A157 实测 `-m 'chore: $(touch PWNED)'` 照抄后 PWNED
    # 被创建：**加了引号的那条，从 msg 这个口子照样可执行**。
    # msg 常来自 CI 拼的 PR 标题（`-m "$(gh pr view --json title)"`），
    # 属外部可控输入。统一走 `_shq()`。
    _paths = " ".join(_shq(p) for p in todo[:20])
    print(f"     git commit -m {_shq(msg)} -- {_paths}")
    # M177（A157，中）：省略号原先**拼进命令行里面**（`+ (" …" if …)`）。
    # bash 里 `…` 不是注释而是一个 pathspec —— 25 个文件时照抄整条失败
    # （`error: pathspec '…' did not match any file(s) known to git`，rc=1），
    # **一个文件都没补提交**，用户会以为"命令跑了但没变化"。
    #
    # 失败点在 21，而"文件多"恰恰是这条建议唯一有价值的场景（3 个文件
    # 时自己敲一遍就行）。也就是说它在需要生效的场合 100% 失效。
    # 同一函数里另外两处截断（上面的 `… 其余 N 个同上`）写对了 ——
    # 省略号在命令**外面**。这里照抄那个写法。
    if len(todo) > 20:
        print(f"     … 其余 {len(todo) - 20} 个未列（命令只带前 20 个，"
              f"其余请另跑一次）")


def _git_env_error(msg, stderr=""):
    """git 失败时给出**可诊断**的报错。

    不能直接把「git 退出码 128」抛给用户：那只会让人去查命令语法，
    而真实成因通常是环境问题，且**每一种都有明确解法**。

    最危险的是「fatal: not a git repository」与「dubious ownership」——
    两者都让 git 返回空，于是 detect_changes()=[]、local_state_map()={}，
    脚本最终打印「本地没有待推送的改动」—— **读不到仓库被伪装成仓库是
    干净的**。用户以为没事，改动其实一个都没推（--direct 模式实测）。
    """
    hint = ""
    low = (stderr or "").lower()
    if "not a git repository" in low:
        hint = (f"\n  当前目录不是 git 仓库：{ROOT}\n"
                f"  本脚本靠 git 检测改动，仓库不可用就无法判断「改了什么」。\n"
                f"  解决：cd 到仓库根目录，或先 git init。")
    elif "dubious ownership" in low:
        hint = (f"\n  仓库属主与当前用户不一致，git 拒绝操作（dubious ownership）。\n"
                f"  这在容器 / 挂载卷场景下极常见：仓库属主是宿主机 uid，"
                f"容器内 euid 不同。\n"
                f"  解决：git config --global --add safe.directory {ROOT}")
    elif "not inside" in low or "is-inside-work-tree" in low:
        hint = (f"\n  {ROOT} 不在 git 工作树内。\n"
                f"  解决：cd 到仓库根目录再运行。")
    return msg + (hint or "\n  git 不可用，无法判断本地改动。") + \
        "\n  ⚠ 不是「没有改动」，是**读不到仓库** —— 请勿据此认为推送成功。"


def _stash_hint():
    """git stash 里还有东西时返回一句提示，否则返回空串。

    `git stash` 之后工作区变干净，detect_changes() 自然为空，脚本只会说
    「本地没有待推送的改动」。改动没丢（在 stash 里），但这句话会让用户
    以为它凭空消失了 —— 尤其是他自己忘了刚 stash 过的时候。

    只在「没有改动」这条路径上提示，平时不打扰；查不到就返回空串，
    绝不因为这条附加信息阻断任何流程。
    """
    try:
        p = subprocess.run(["git", "-C", ROOT, "stash", "list"],
                           capture_output=True, text=True,
                           timeout=DEFAULT_GIT_TIMEOUT)
        if p.returncode != 0:
            return ""
        n = len([l for l in (p.stdout or "").splitlines() if l.strip()])
        if not n:
            return ""
        return (f"  ! git stash 里还有 {n} 条改动（未丢失）："
                f"`git stash list` 查看，`git stash pop` 取回。\n"
                f"    工作区此刻是干净的吗？你可能是 stash 之后忘了 pop。")
    except (OSError, subprocess.SubprocessError):
        # N564（第一百八十四轮，低危）：上面是 `except BaseException` ——
        # 它会连 KeyboardInterrupt / SystemExit 一起吞掉。
        # docstring 声明「绝不因为这条附加信息阻断任何流程」是有意的，
        # 但达到同一目的只需 BaseException 的**真子集**：这里没有任何
        # 会抛 SystemExit 的调用，Ctrl-C 必须能穿透。
        return ""


def branch_protection(branch):
    """查分支保护配置；未开启返回 None，查询失败返回 False（未知）。

    三态而不是两态：
      · dict  → 确实开了保护
      · None  → 确认没开（GitHub 对未保护分支返回 404）
      · False → **查不出来**（403 权限不足 / 网络错 / 响应异常）
    把「查不出来」和「没开」混成一种，会让无权限的用户以为可以直推，
    结果撞一个看不懂的 403 —— 这正是本函数想避免的事。
    """
    try:
        # 不能用 _ref_path()：那是 /git/refs/heads/... 的**完整路径**，
        # 而 /branches/{branch}/protection 要的是**分支名**（URL 编码后）。
        # 分支名含 / 时必须保留（quote 的 safe="/"），否则 task/xxx 变
        # task%2Fxxx → 404 → 被当成「没开保护」放行。
        from urllib.parse import quote
        return api("GET",
                   f"/branches/{quote(branch, safe='/')}/protection",
                   allow_404=True)
    except BaseException:
        return False


def _refuse_direct_if_protected(branch, from_cli=False):
    """--direct 且主干开了 PR 保护时提前拒绝，别让用户撞看不懂的 403。

    from_cli：本次的 direct 是命令行 --direct 带来的（True），
    还是状态文件里持久化的（False）。只影响提示里给出的解除方式 ——
    后者没有 --direct 可以"去掉"，得给真正能走通的路（M114）。

    开启「Require a pull request before merging」后，服务端会直接拒绝
    直推主干。此时 --direct 的所有优势（少几次 API、历史线性）都拿不到，
    只剩一个含糊的 403。早点说清楚，用户好改用默认 PR 流程。
    """
    prot = branch_protection(branch)
    if prot is None:
        return                                  # 确认没开保护，放行
    if prot is False:
        # 查不出来时**不拦**，但要说一声：静默放行等于把 403 留给用户猜。
        #
        # M114（A108，复核 N441）：这里是裸 print，而本函数在**分派之前**
        # 被调用 —— 脚本自己立的判据是「纯数据流模式（--json / --mine）下
        # 分派前的任何输出都必须静音」。实测 `--status --json` 在这条分支上
        # 会先打两行文本、再打 JSON → json.loads 失败，而退出码仍是 0。
        # 与已修的 N99 / M61 / M81 完全同族，只是这一处没跟上。
        #
        # N386：注释里这条判据自己写的是「--json **/ --mine**」，而门控
        # 只覆盖了前者。本函数当前只在推送侧调用（`--scope --mine` 走不到），
        # 但同一份文件里的同一条规则不该有两套写法 —— 与 4323 / 14200
        # 一起收敛到 `_stream_active()`。
        if not _stream_active():
            print(f"  ! 无法确认 {branch} 是否开启了分支保护"
                  f"（权限不足或网络问题）。\n"
                  f"    若仓库开启了「Require a pull request」，"
                  f"本次直推会被服务端拒绝。")
        return
    # 判据必须用**键存在性**，不能用真值。
    #
    # GitHub 开启「Require a pull request」时 required_pull_request_reviews
    # 常常就是 {}（内部无附加要求）—— 空字典是 falsy，用 `or` 一判断就
    # 漏掉，保护开了却当成没开（实测：主干被直推成功）。
    # 这里任何一类保护存在即意味着「直推会被拒」，不需要细分。
    try:
        KEYS = ("required_pull_request_reviews", "required_status_checks",
                "enforce_admins", "restrictions", "required_signatures",
                "required_linear_history", "allow_force_pushes",
                "allow_deletions", "required_conversation_resolution")
        # N443（真修法，fail-closed）：`k in prot` 对 list 查**成员**、
        # 对 str 查**子串** —— 两者都可能为真，于是"响应里出现保护键"
        # 这个判据在非 dict 上照样成立，而它本该是 dict 的语义。
        #
        # 所以判据的顺序必须是：先确认它是 dict，再查键。
        #
        # M114（A108）删掉了原先那两行 isinstance，只覆盖了"含保护键的
        # 非 dict"。下面这处 `if not isinstance(...)` 是**后来补的**，
        # 把所有非 dict 一并拦下。
        #
        # ⚠ 原先这里还写着一句「**不含**保护键的 `["foo"]` 仍走
        # any()=False → 放行，`42` 仍走 TypeError → 放行，剩下的还是
        # fail-open」—— **这句话现在是错的**（第一百六十二轮实测更正）：
        #   · `["foo"]` / `42` 在 3304 处就被拦下（不会再走到 any()）；
        #   · `42` 那条当初也写错了：即便没有 3304，`"k" in 42` 抛的
        #     TypeError 会被下面 `except TypeError` 接住并拦下，不是放行。
        # 注释停在"修复前/修复中"的形态与 M63、N100 同族 —— 它描述的
        # 是 M114 刚落地时的中间状态，比没有注释更误导。
        #
        # 契约是 None / False / dict 三态 —— 前两态上面已 return，
        # 走到这里却不是 dict，说明响应已越出契约（代理改写、非标准
        # 实现），不该据此放行**直推主干**。判不了就停下来问。
        if not isinstance(prot, dict):
            _blocked(
                f"❌ 无法判断 {branch} 的分支保护状态："
                f"响应不是对象（得到 {type(prot).__name__}）。\n"
                f"   放行直推主干可能撞上服务端的保护规则，也可能保护根本没开"
                f" —— 两者都无法从这份响应区分，所以停下来。\n"
                f"   若该分支确实未受保护，请确认 /branches/{branch}/protection"
                f" 返回的是对象，或用默认 PR 流程推送。")
        if not any(k in prot for k in KEYS):
            return                              # 响应里没有任何保护项
    except TypeError:
        # ⚠ 当前**不可达**：上面 3304 已保证走到 `any()` 时 prot 是 dict，
        # 而 `"k" in dict` 不抛 TypeError。保留作防御（若将来在上面新增
        # 了对 prot 的其它操作），不删 —— 与 A3 的 G2 同一处置：
        # 不可达的代码留着无害，删掉后若控制流一变就没人补回来。
        # 同上：不是"不猜"就能放行的场景 —— 直推主干被拒的代价更高。
        _blocked(
            f"❌ 无法判断 {branch} 的分支保护状态：响应结构异常"
            f"（类型 {type(prot).__name__}，无法枚举保护项）。\n"
            f"   不放行直推主干；请改用默认 PR 流程。")
    #
    # M114（A108，复核 N443）：原先这里有两行 ——
    #     if not isinstance(prot, dict):
    #         return
    # 而 `any()` 已经先跑过了：唯一能走到这里的情形，恰恰是
    # **响应里出现了保护键**，它却把「拦下」改成「放行」。
    # 判据的效果与它想防的方向相反（任何非 dict 响应上恒为 fail-open）。
    #
    # 报告给的修法是"把 isinstance 上移到 any() 之前" —— 实测**无效**：
    # 上移之后 list / str 响应同样 return，行为一字不变（两条路都放行）。
    # 真正的选择只有两个：非 dict 一律放行（等于现在的样子），
    # 或按 fail-closed 处理。本脚本一贯取后者（判不了就停下来问），
    # 且 branch_protection 的契约就是 None / False / dict 三态 ——
    # 走到这里却不是 dict，说明响应已越出契约，不该据此放行直推。
    # 所以删掉那两行：含保护键的响应一律拦下。
    # M114（A108，复核 N440）：原文案写死「去掉 --direct 即可」，
    # 而 wf 可能来自**状态文件**（`state["workflow"] == "direct"`）——
    # 那时命令行里根本没有 --direct 可以去掉，且**没有任何 CLI 能切回 pr**
    # （无 --pr / --workflow 选项；--branch 指定的是任务分支，改不到它）。
    # 实测 `--status` 在 workflow=direct 时拿到这句提示，照它做唯一能做的
    # 就是把 --direct 从一条不含它的命令里"去掉" —— 做不到。
    if from_cli:
        _howto = ("   改用默认 PR 流程（去掉 --direct 即可）：\n"
                  '     python3 push_api.py -m "<消息>"')
    else:
        _howto = ("   基线里 workflow 已持久化为 direct，"
                  "而**没有 CLI 能切回 pr**。真正能走通的两条：\n"
                  "     python3 push_api.py --reset-baseline    # 重建基线\n"
                  f"   或手工改 {STATE_PATH} 里的 workflow 字段为 \"pr\"")
    _blocked(
        f"❌ {branch} 已开启分支保护，--direct（直推主干）会被服务端拒绝。\n"
        f"   直推主干的优势（少几次 API 请求、历史线性）在开启保护后都不存在了。\n"
        f"{_howto}\n"
        f"   若确认要直推，请先在仓库设置里关闭 "
        f"「Require a pull request before merging」。")


def require_git_repo():
    """启动即校验 git 可用。不可用直接中止，不让流程走到「误判无改动」。

    放在**最前面**（在拉远端状态之前）：git 不可用时后面全是无用功，
    而且越晚报错，用户越容易把「没推上去」当成「已经推了」。
    """
    try:
        proc = subprocess.run(["git", "-C", ROOT, "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True,
                              timeout=DEFAULT_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise SystemExit(_git_env_error(
            f"git rev-parse --is-inside-work-tree 超时（>{DEFAULT_GIT_TIMEOUT}s，{ROOT}）", ""))
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise SystemExit(_git_env_error(
            f"git rev-parse --is-inside-work-tree 失败（{ROOT}）",
            proc.stderr or ""))


def _inside_root(real_path):
    """realpath 之后的路径是否仍在仓库内 —— **强**越界判据。

    N633（A128，低危）：这里原先写的是「**唯一**的越界判据」，而本文件里
    还有第二份 `_safe_rel()`（弱判据：只挡绝对路径与 `..`，不解析符号链接，
    因此会放过 `etcdir/passwd` 这类穿出仓库的软链路径）。两份名字只差一个
    下划线、docstring 里又互不指名，于是"唯一"是**假的** ——
    它会诱导后人以为只有一份，从而把弱判据用在需要强判据的场景。

    A128 实测的函数级分叉（`etcdir -> /etc` 软链）：

        路径              safe_rel（强）   _safe_rel（弱）
        a.txt             a.txt           True
        etcdir/passwd     None ★ 拦下     True  ★ 放行
        ../outside.txt    None            False
        sub/../a.txt      a.txt           False ★ 连合法路径也误杀

    弱判据当前的调用点：删除清单过滤与搬家记录的旧路径校验。
    **是否换成强判据属产品决策，本轮不做**（A128 也没构造端到端实证）：
    弱版对 `sub/../a.txt` 的误杀说明它不是"强的弱化版"而是**另一套口径**，
    直接替换会同时改变放行与拒绝两侧，必须单独验。
    本轮只做两件低成本的事：把"唯一"改掉、让两份互相指名。

    必须抽成函数、让读侧（safe_rel）与写侧（_write_local 的 symlink 目标
    校验）共用：这两处原本各写一遍「路径在不在仓库内」，左边用
    realpath(ROOT) 比、右边拿裸 ROOT 比 —— 一边解析一边不解析。

    于是仓库根本身是软链时（容器挂载卷 / CI 工作区的常态）出现不对称：
    同一个合法路径，读侧放行、写侧判成「指向仓库外」并 SystemExit，
    而 SystemExit 会中断**整个** --pull，不只是跳过这一个文件（实测复现）。

    基准统一取 realpath(ROOT)：leading 软链解析掉之后，两侧才有可比性。
    """
    root = os.path.realpath(ROOT)
    return real_path == root or real_path.startswith(root + os.sep)


def safe_rel(rel):
    """把用户/脚本给的路径规范成仓库内相对路径；越界的返回 None。

    越界校验必须用 realpath 而非 normpath：normpath 只做字符串规整，
    会放过「仓库内指向外部的符号链接」——`etcdir -> /etc` 时
    `etcdir/passwd` 能通过 normpath 检查，open() 却读到 /etc/passwd。

    但返回值仍按 normpath 算，避免把 `link`（合法内部链接）改写它的目标路径。
    """
    if not rel or os.path.isabs(rel) or rel.startswith("~"):
        return None
    full = os.path.normpath(os.path.join(ROOT, rel))
    out = os.path.relpath(full, ROOT)
    if out == ".." or out.startswith(".." + os.sep):
        return None
    real = os.path.realpath(full)
    if not _inside_root(real):
        return None
    return out


def local_blob_sha(rel):
    """本地文件的 git blob sha（与 GitHub 的 blob sha 同一算法）

    必须用 `--no-filters`。

    `git hash-object <路径>` 会按 .gitattributes 应用 **clean filter**
    （CRLF 归一化、LFS 的指针替换等），算出的是「入库形态」的 sha；
    而本脚本上传的是工作区**原始字节**，GitHub 也照原样存。
    两者不一致 → 状态里记的 sha 与远端实际 blob sha 对不上 →
    下次推送误判「远端被他人改动」。

    实测（.gitattributes = `* text=auto eol=crlf`，工作区 CRLF）：
        git hash-object a.txt              → 94954abd（LF 归一化后）
        git hash-object --no-filters a.txt → 23eb407b（原始 CRLF）
        远端实际收到的是 CRLF，blob sha = 23eb407b
    用第一个就会永久误报。加 --no-filters 后 sha 恒等于所传字节的哈希。

    代价：当仓库配了 filter 时，本脚本推送的内容与 `git push` 的不一致
    （git 会存归一化后的，我们存原始字节）。这个差异由
    warn_if_filters_active() 显式告知，不静默。

    符号链接特殊处理：git 存的 symlink blob 内容是「目标路径字符串」，
    而 hash-object 默认会跟随链接读取目标文件内容，两者 sha 不一致。
    """
    full = os.path.join(ROOT, rel)
    if os.path.islink(full):
        # symlink 目标是**任意字节**（Linux 只排除 / 和 NUL），可以是非法
        # UTF-8。text=True 下 subprocess 用严格 UTF-8 编码 stdin，
        # 遇 surrogate 直接抛 UnicodeEncodeError —— 而这是**推送路径**上
        # 的调用，崩在 halfway 会留下脏的远端状态。
        # 用 surrogateescape 双向还原原始字节；去掉 text=True 后 stdout
        # 是 bytes，需显式 decode。
        raw = os.readlink(full).encode("utf-8", "surrogateescape")
        try:
            out = subprocess.run(["git", "-C", ROOT, "hash-object", "--stdin"],
                                 input=raw, capture_output=True,
                                 timeout=DEFAULT_GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            # 不能返回空串兜底：调用方会把 "" 当成真实 sha 写进基线，
            # 于是「本地 sha ≠ 远端 sha」永久成立 —— 又一个永久误报。
            raise SystemExit(_git_env_error(
                f"git hash-object --stdin 超时（>{DEFAULT_GIT_TIMEOUT}s）：{rel}", ""))
        # N427（第一百三十九轮，中危）：上面只处理了**超时**一种故障，
        # 而出口既不查 returncode、也不做 40 位校验 —— 于是 rc=1 /
        # stdout 空时静默返回 ""。同一个函数、同一类「拿不到 sha」故障：
        #
        #   普通文件分支 → 抛 SystemExit（M88 加的形态校验）
        #   symlink 分支 → 返回 ''          ★ 静默
        #
        # 实测（故障注入：让 git hash-object --stdin 返回 rc=1 / stdout 空）：
        #   plain.txt → SystemExit: 算不出 blob sha（…返回 ''）
        #   link.txt  → lmap['link.txt'] = mode=120000 sha=''（静默）
        #
        # 本函数上面那段注释已经立过「不能返回空串兜底」这条原则，
        # 这里补的是**另一半**—— 普通文件分支在 M88 时补上了，symlink
        # 分支一直没补。
        _so = out.stdout or b""
        _sout = (_so if isinstance(_so, str)
                 else _so.decode("utf-8", "replace")).strip()
        if out.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", _sout):
            _err = out.stderr or b""
            if not isinstance(_err, str):
                _err = _err.decode("utf-8", "replace")
            _err = _err.strip()
            raise SystemExit(_git_env_error(
                f"算不出符号链接 {rel} 的 blob sha（git hash-object --stdin "
                f"rc={out.returncode} 返回 {_sout[:40]!r}"
                + (f"，stderr: {_err[:120]}" if _err else "")
                + f"），无法判断本地内容是否变化", ""))
        return _sout
    # M88（A90）：以 `-` 开头的路径会被 git 当成**选项**。
    #
    # 实测（`git hash-object --no-filters -abc.txt`，无 `--`）：
    #     rc=129   stderr: error: unknown switch `a'   stdout: ''
    # 而 git() 在非 0 且未传 fatal 时**只打印一行警告、照常返回 stdout**，
    # 于是这里拿到空串；调用方把它当成真实 sha 写进基线 →
    # 「本地 sha ≠ 远端 sha」从此**永久成立**，该文件永久卡在待推列表里。
    #
    # 批量路径（--stdin-paths）不受影响：路径走 stdin，不进 argv。
    # 所以这个洞只在**单个**调用时出现 —— 而 len(rels)<=1、
    # 批量失败回退、symlink 之外的兜底，走的都是单条路径。
    #
    # 加 `--` 让 git 停止解析选项；并对结果做形态校验：
    # 拿不到 sha 就不返回。空串比报错危险得多 —— 本函数上面那段注释
    # （TimeoutExpired 分支）已经立过这条原则，这里补上另一半。
    _out = (git("hash-object", "--no-filters", "--", rel) or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", _out):
        raise SystemExit(_git_env_error(
            f"算不出 {rel} 的 blob sha（git hash-object 返回 "
            f"{_out[:40]!r}），无法判断本地内容是否变化", ""))
    return _out


def local_blob_shas(rels):
    """批量算 git blob sha：一次子进程处理所有路径。

    逐个调 local_blob_sha() 时 N 个文件 = N 次 `git hash-object` 子进程。
    沙盒里 git 单次调用可到 1~5 秒，100 个改动文件就是几分钟 ——
    而这纯粹是可避免的进程开销。

    用 `--stdin-paths`：从 stdin 读路径列表，一次给出所有 sha（按输入顺序）。
    失败时**回退**到逐个计算：批处理下单个路径出问题会让整批失败，
    而逐个模式坏的会被跳过、其余仍然可用。
    """
    if len(rels) <= 1:
        return {r: local_blob_sha(r) for r in rels}
    # 含换行的路径不能进批量：--stdin-paths 按行读，一个文件会被拆成
    # 两行，靠行数兜回逐个模式（安全，但批量优化对该类仓库永不生效；
    # 若拆行数与失败数恰好抵消，还会贴错 sha）。
    batch = [r for r in rels if "\n" not in r]
    try:
        proc = subprocess.run(
            ["git", "-C", ROOT, "hash-object", "--no-filters", "--stdin-paths"],
            input="\n".join(batch), capture_output=True, text=True,
            errors="surrogateescape", timeout=DEFAULT_GIT_TIMEOUT)
        if proc.returncode == 0:
            out = proc.stdout.split()
            # 长度必须对 **batch**（喂进去的个数），不是 rels。
            # 用 rels 比对时，只要有一个含换行的路径两者就永远不等，
            # 于是每次都白跑一次注定失败的批量再退回逐个 —— 批量优化
            # 对这类仓库形同虚设，正是这条修复要治的。
            # N427（同族，同一轮）：`len(out) == len(batch)` 是**数量**
            # 相等，不是形态合法。当前 git hash-object --stdin-paths 正常时
            # stdout 只有 sha 行所以不触发；但这条闸门本身就是缺的 ——
            # 换 git 版本 / 输出混进告警就可能失效，而失效的后果与上面
            # symlink 那支完全相同（空串/非 sha 被当成真实 sha 写进基线）。
            if len(out) == len(batch) and all(
                    re.fullmatch(r"[0-9a-f]{40}", x) for x in out):
                got = dict(zip(batch, out))
                for r in rels:
                    if r not in got:
                        got[r] = local_blob_sha(r)
                return got
    except subprocess.TimeoutExpired:
        pass
    return {r: local_blob_sha(r) for r in rels}


#: N219：`_check_attr_batch` 失败时记下的原因（供调用方给出准确提示）。
#: 失败与"确实没有属性"必须用**不同的返回值**区分 —— 都返回 [] 的话，
#: 上层无法分辨，只能按"没有 LFS"放行，那正是 fail-open。
_ATTR_CHECK_FAILED = []


def _check_attr_batch(files):
    """批量查 git 属性，走 stdin（-z）而不是 argv 展开。

    `git check-attr ... -- *files` 在上万个路径时会触碰 ARG_MAX（典型 2MB），
    触发 OSError(E2BIG) —— 而 git() 只捕获 TimeoutExpired，**不捕获 OSError**，
    于是裸 traceback 直接冒到用户面前（实测 20000 个路径复现）。

    与 _git_add_paths()（≥200 路径改走 --pathspec-from-file）和
    gitignored_set()（--stdin -z）保持同一口径：凡是「路径数可能很大」
    的 git 调用都不走 argv。口径不一致时，只有最先被撞到的那处会被修，
    其余两处继续潜伏 —— 这正是本函数存在的原因。

    编解码要**双向** surrogateescape（stdin 与 stdout 都要）：路径可能含
    非 UTF-8 字节，只修输出不修输入，崩溃只是换个地方。完整说明见
    gitignored_set()。

    返回 [(path, attr, value)] —— 由调用方自己组装，本函数不假设查询了
    哪些属性。
    """
    if not files:
        return []
    data = ("\0".join(files) + "\0").encode("utf-8", "surrogateescape")
    try:
        proc = subprocess.run(
            ["git", "-C", ROOT, "check-attr", "-z", "--stdin",
             "text", "eol", "filter"],
            input=data, capture_output=True, timeout=DEFAULT_GIT_TIMEOUT)
        raw = proc.stdout.decode("utf-8", "surrogateescape")
    except (OSError, subprocess.SubprocessError, UnicodeError) as e:
        # N219（第一百〇七轮）：这段注释原本逐字写着
        #   "查不到属性**不能**当「没有 LFS」放过 —— LFS 指针文件被当
        #    普通内容推上去会绕过 LFS"
        # 而它下一行 `return []` 做的**恰恰就是**当「没有 LFS」放过：
        # 上层拿到空 rows → attrs 全空 → 不进 lfs 分支 → 不抛 → 静默放行。
        #
        # 实测（.gitattributes 写了 filter=lfs、注入 check-attr 超时）：
        #   ★ 未拒绝 → 静默放行（内容会绕过 LFS 塞进仓库）
        #
        # 这是**注释与实现相反**，且反的方向是 fail-open：
        # 不报错、不拦下、推送成功，但远端拿到的是指针文本而非实际内容
        # —— 是这批里最隐蔽的一类（错误不表现为失败，表现为"成功但错"）。
        #
        # 改成 fail-closed：用 **None** 做哨兵（与"确实没有属性"的 []
        # 区分开），由调用方硬拦。
        #
        # 会不会误伤？check-attr 失败通常意味着 git 本身不可用，而推送
        # 早先已过 require_git_repo()。所以这里拦下的是"git 在但属性
        # 查询异常"的少数场景，宁可让人重试也不要静默绕过 LFS。
        _ATTR_CHECK_FAILED.append(str(e))
        print(f"  ! git check-attr 失败（{e}），无法检出 LFS / 规范化属性")
        return None
    # -z 的输出是 NUL 分隔的 **path / attr / value 三元组**，不是
    # `<path>: <attr>: <value>` 行。沿用按行解析会拿到一串残缺数据，
    # 而且不报错 —— LFS 检查静默失效，比崩溃更难发现。
    f = [x for x in raw.split("\0")]
    rows = []
    for i in range(0, len(f) - 2, 3):
        rows.append((f[i], f[i + 1], f[i + 2]))
    return rows


def warn_if_filters_active(files):
    """待推文件若受 .gitattributes 的 filter / 换行归一化影响，必须告知。

    用 --no-filters 求 sha 保证了「状态里的 sha == 远端实际 blob sha」，
    但副作用是本脚本推的是**原始字节**，与 `git push`（会存 clean 之后的
    内容）不一致。这个差异必须让人知道，否则用户以为两者等价。

    Git LFS 是**必须拒绝**的特例：clean filter 会把它变成一个小指针文件，
    而我们若上传原始字节，等于把整个大文件塞进 git 仓库、绕过 LFS ——
    仓库体积失控，且其他 clone 的人拿到内容与 LFS 不一致。

    批量查询（一次 git 调用），不为每个文件跑一遍 —— 上百个文件时
    逐个 subprocess 的开销实测很可观。
    """
    if not files:
        return
    rows = _check_attr_batch(files)
    if rows is None:
        # N219：检测不到 ≠ 没有 LFS。判不出就**不放行** ——
        # LFS 指针被当普通内容推上去等于绕过 LFS（大文件进仓库、
        # 别人 clone 拿到的内容与 LFS 不一致），且**不表现为失败**，
        # 表现为"推送成功但内容是错的"。
        why = _ATTR_CHECK_FAILED[-1] if _ATTR_CHECK_FAILED else "未知"
        _blocked(
            f"无法检出 .gitattributes 属性，拒绝推送（N219）\n"
            f"  原因：{why}\n"
            f"  判不出「是否有 LFS / 规范化属性」时不能当没有 —— "
            f"LFS 文件被当普通内容上传会绕过 LFS。\n"
            f"  处理：确认 git check-attr 可用后重试；"
            f"若这些文件确由 LFS 管理，请用 git 命令行推送。")
    lfs, normalized = [], []
    by_path = {}
    for path, attr, val in rows:
        by_path.setdefault(path, {})[attr] = val
    for p, attrs in by_path.items():
        if attrs.get("filter") not in (None, "unspecified", ""):
            if "lfs" in attrs["filter"]:
                lfs.append(p)
            else:
                normalized.append((p, f"filter={attrs['filter']}"))
        elif attrs.get("text") not in (None, "unspecified", "", "auto"):
            normalized.append((p, f"text={attrs['text']}"))
        elif attrs.get("eol") not in (None, "unspecified", ""):
            normalized.append((p, f"eol={attrs['eol']}"))

    if lfs:
        _blocked(
            f"以下文件由 Git LFS 管理，本脚本拒绝推送：{sorted(lfs)[:10]}\n"
            f"  LFS 的 clean filter 会把内容换成一个小指针文件，"
            f"而这里只能上传原始字节 —— 等于绕过 LFS 把大文件塞进仓库。\n"
            f"  请用 git 命令行推送这些文件。")

    if normalized:
        print(f"\n  ! {len(normalized)} 个文件受 .gitattributes 影响"
              f"（换行归一化 / 自定义 filter）：")
        for p, why in normalized[:5]:
            print(f"     - {p}: {why}")
        print("    本脚本上传的是**工作区原始字节**，与 `git push`"
              "（会存 clean 之后的内容）不同。")
        print("    远端内容与你本地一致，但与 git 仓库里的形态可能不同。")


# 树缓存：{ref_sha: {path: (mode, sha)}}。
# 一次推送里同一棵树会被拉 2~3 次（新分支时 base_sha == head_sha，
# 3355 行和 3002 行拉的是同一棵），每次都是一次 recursive=1 的全量请求。
# 大仓库下这笔开销可观，且是纯浪费 —— 同一个 commit 的树不会变。
# _TREE_CACHE = {}  # → Runtime 实例属性（见 Runtime 段）


def remote_state_map(ref_sha, use_cache=True):
    """一次性取远端整棵树的 path → (mode, sha)

    mode 一起取：只比 sha 会漏掉「只改了可执行位」的变更（见 T-11）。

    use_cache：同一 ref_sha 在进程内复用结果。缓存**只按 commit sha 索引**，
    因为 git 的树是内容寻址的 —— 同一个 commit 的树不可能变化。
    需要强制刷新（比如刚推送完想看新状态）时显式传 use_cache=False。
    """
    if use_cache and ref_sha in _RT.tree_cache:
        return _RT.tree_cache[ref_sha]
    tree = api("GET", f"/git/trees/{ref_sha}?recursive=1")
    if tree.get("truncated"):
        # M75（A79）：N361 把 16 处「拦下」语义收敛进 `_blocked()`，漏了这里。
        #
        # 判据是**语义**不是措辞：这一处是"判不了所以不推"，改动一个没丢，
        # 正是 GuardBlock docstring 里 `2` 的定义。裸 SystemExit 会给 1，
        # 与"参数错误""网络失败"同码 —— CI 要么为一个"改动好好在本地"
        # 的拦截告警（噪音），要么为压噪音把 1 一并忽略（真出错时漏报）。
        #
        # 紧邻下一行（取树失败）**保持** SystemExit：那是请求没做成，
        # 属"其它错误"。两者文案相近、语义相反，别一起改。
        #
        # ⚠ `_blocked()` 形态是**调用即抛**，调用点不写 `raise`
        #（写成 `raise _blocked(...)` 的那个 raise 永远执行不到）。
        _blocked("远端 tree 被截断（仓库过大），无法做覆盖校验，已中止")
    if "tree" not in tree:
        # 别静默降级成空表：那会让每个文件都落到「基线未记录」分支，
        # 表现得像「疑似他人改动」，把网络/限流问题伪装成冲突。
        raise SystemExit(f"取远端 tree 失败: {json.dumps(tree, ensure_ascii=False)[:300]}")
    got = {t["path"]: (t.get("mode", "100644"), t["sha"])
           for t in tree["tree"] if t["type"] == "blob"}
    if use_cache:
        _RT.tree_cache[ref_sha] = got
    return got


def _corrupt_state(why, path=None):
    """状态文件非法：先把原文件留一份副本，再中止。

    必须先备份再报错：用户看到这条错误的下一步多半就是 --reset-baseline，
    那会直接覆盖原文件。留一份 .corrupt 既让他能回头检查，也不挡重建的路。
    """
    path = path or STATE_PATH
    backup = None
    try:
        # 只备份**普通文件**：目录会抛 OSError（已捕获）；FIFO 则更糟——
        # copy2 会去读它，然后永远阻塞在那里，连这条错误都打印不出来。
        if os.path.isfile(path) and not os.path.islink(path):
            # N413（低危）：原先恒写 `path + ".corrupt"`，连续损坏两次
            # 时第二次覆盖第一次的现场 —— 备份的意义正是"能回头检查"，
            # 覆盖掉就只剩最后一次，而第一次往往才是根因。
            # 已存在就带序号另存（.corrupt, .corrupt.1, .corrupt.2 …）。
            backup = path + ".corrupt"
            if os.path.exists(backup):
                _i = 1
                while os.path.exists(f"{backup}.{_i}"):
                    _i += 1
                backup = f"{backup}.{_i}"
            shutil.copy2(path, backup)
    except OSError:
        backup = None
    hint = f"\n  原文已备份到 {backup}" if backup else ""
    raise SystemExit(
        f"{path} 内容不合法：{why}\n"
        f"  基线是「远端文件是否被他人改动」的唯一参照，"
        f"结构不对时继续跑会误判，因此必须停下。{hint}\n"
        f"  处理：确认无需保留后删除该文件，再跑 `python3 push_api.py --init-baseline`\n"
        f"  （重建后文件级防护会短暂失效，重建后建议先 --pull 同步一次）"
    )


def validate_state(state, path=None):
    """校验基线结构，合法则原样返回；非法则备份 + 中止。

    只拦 JSON **语法**错误远不够：语法合法但结构不对的状态文件
    （整个 state 是个数字、files 是个列表、conflicts 是个字符串……）
    会让后面的 .get() 直接 AttributeError 崩在半路，堆栈对排查毫无帮助。

    更危险的是 JSON 的 `null`：json.load 返回 None，而 load_state 用
    None 表示「文件不存在」—— 于是坏状态被当成首次使用，脚本会高高兴兴
    地引导用户重建基线，把唯一能判断「远端是否被改过」的参照**静默**丢掉。
    所以结构校验必须拦在返回 None 之前。

    逐项校验而非 try/except AttributeError：后者只能发现「用到才崩」的字段，
    没走到的分支依旧带着坏数据运行（conflicts 是字符串时就会这样）。
    """
    if not isinstance(state, dict):
        _corrupt_state(f"期望一个 JSON 对象，实际是 {type(state).__name__}", path)

    def chk(name, types, optional=False, exact=False):
        if name not in state:
            if optional:
                return
            _corrupt_state(f"缺少必需字段 {name!r}", path)
        v = state[name]
        if v is None and optional:
            return
        if exact and isinstance(v, bool) and bool not in (
                types if isinstance(types, tuple) else (types,)):
            # bool 是 int 的子类：`version: true` 能通过 int 校验。
            # 无害但语义不严谨，与整套状态校验的严谨风格不一致。
            _corrupt_state(f"{name!r} 不接受布尔值（实际是 {type(v).__name__}）", path)
        if not isinstance(v, types):
            want = "/".join(t.__name__ for t in types) if isinstance(types, tuple) else types.__name__
            _corrupt_state(f"{name!r} 应是 {want}，实际是 {type(v).__name__}", path)

    # bool 是 int 的子类，`version: true` 会被当成合法版本号。
    # 这里显式排除，与整套「18 种坏状态全拦」的严谨风格保持一致。
    chk("version", int, optional=True, exact=True)
    chk("files", dict)
    chk("tasks", dict, optional=True)
    chk("conflicts", list, optional=True)
    for k in ("base_commit", "synced_commit", "branch", "remote", "task_branch"):
        chk(k, str, optional=True)
    chk("pr_number", int, optional=True)
    # N46：这三个字段此前完全不校验。
    #   local_seen —— 判据 3 的依据，被污染成字符串会让删除永久静默失效
    #                 （N23 实测：set("a.txt") 得到字符集合）。
    #   local_head —— 回退检测的基准，类型错了会让检测静默失效。
    #   workflow   —— 决定 direct / PR 两条完全不同的路径（N36），
    #                 非法值若在别处被写入会静默改变行为，而现有报错
    #                 只在取到非法值时才触发。
    chk("local_seen", list, optional=True)
    # N381：远端独有清单。类型坏了必须按「无」处理（漏报），绝不能按
    # 「全都是」处理 —— 那等于把判据 3 重新打开（误删）。
    chk("remote_only", list, optional=True)
    # N382：local_seen 裁剪留痕（纯审计）。坏了按「无」处理即可，
    # 它不参与任何判据，丢掉只是少一条追溯线索。
    chk("local_seen_cut", list, optional=True)
    # N410（第一百三十二轮，中危）：这里原先只 `chk(..., str)`，
    # 于是 'HEAD' / '@' / 'master' / 'HEAD@{0}' / 7 位短 sha **全部通过**。
    #
    # 而 `git merge-base --is-ancestor HEAD <cur>` 会被 git 解析成
    # `--is-ancestor <cur> <cur>` → rc 恒 0 → 恒判"正常前进"：
    #   真实仓库 C1→C2 后 reset --hard C1（用户把本地回退了）：
    #     local_head='HEAD' → 回退=False  ★ 放行
    #     local_head=C2     → 回退=True    拦下
    #   **同一个回退动作，结论完全相反。**
    #
    # 第零层是唯一能发现「本地悄悄回到旧版本」的闸门（synced_commit 那层
    # 在 reset --hard 后不变，见 P0-1 的 docstring 例子）。
    #
    # N292 修的是**写入侧**（_local_head() 校验 40/64 位 hex），写入侧确实
    # 干净了；它 docstring 里列的第 ③ 条就是"读取侧这行照常放行"——
    # 原样留了三轮。读侧必须同源，否则旧状态文件（N292 之前写入的）
    # 让第零层**永久失效且无声**。
    #
    # 与 _local_head() 同源：非 sha 形态按缺失处理（fail-open 但**有声**，
    # 走 P1-1 提示，并会在下次成功同步后自动启用）。
    _lh = state.get("local_head")
    if _lh is not None and not (isinstance(_lh, str) and _is_sha(_lh)):
        state.pop("local_head", None)
    chk("local_head", str, optional=True)
    chk("workflow", str, optional=True)

    ver = state.get("version")
    if isinstance(ver, int) and ver > STATE_VERSION:
        _corrupt_state(
            f"基线版本 {ver} 高于本脚本支持的 {STATE_VERSION}"
            f"（由更新版本的脚本写入），请升级脚本后再用", path)

    for p, v in state["files"].items():
        # v1 基线存的是裸 sha 字符串，v2 存 {mode, sha}；两者都由
        # migrate_state 收敛，这里只挡完全无法解释的类型。
        if not isinstance(v, (dict, str)):
            _corrupt_state(
                f"files[{p!r}] 应是对象或字符串，实际是 {type(v).__name__}", path)
        # v2 形态还要校验**字段类型**：只查 dict 的话，
        # {"mode": 100755, "sha": 123}（JSON 里写成数字）能过校验，
        # 然后在 `(lmode, lsha) == (rmode, rsha)` 比较时永远为假 →
        # 永久误报「远端被他人改动」，且没有任何提示指向真正的原因。
        # 与 version 字段用 exact=True 挡掉 bool 是同一个严格度要求。
        if isinstance(v, dict):
            for f in ("mode", "sha"):
                if f in v and not isinstance(v[f], str):
                    _corrupt_state(
                        f"files[{p!r}][{f!r}] 应是字符串，实际是 "
                        f"{type(v[f]).__name__}", path)
            # A7-K2（第一百六十二轮）：mode 的**取值**此前完全不校验。
            # `VALID_MODES` 全文只在定义处与 local_state_map 的一次回退里
            # 出现，`validate_state` 从不检查 —— 于是 `mode='999999'`、
            # `mode=''` 都能进基线，随后在
            #   `(lmode, lsha) == (rmode, rsha)`
            # 里永远不相等 → 该文件被**永久**判成「远端已被他人改动」，
            # 报错指向 --force-overwrite，而根本没人改过。
            #
            # 与已修的 autocrlf / symlink sha 分叉是同一症状（把用户推向
            # 摧毁防护），入口换成了"基线被写坏"。
            #
            # ⚠ **只校验取值，不要求 mode 必须存在**：
            #   · 缺 mode 是合法的 v1.5 过渡态 —— `_baseline_stale()` 对
            #     无 mode 的基线记录会退化为只比 sha，判损坏会误伤。
            #   · 缺 sha / 空对象 {} 不在这里判损坏：那是 N54 定义的
            #     「空记录」，--status 的基线健康度**专门报告**它并给出
            #     --init-baseline 出路。判成损坏会让一份可恢复的基线
            #     直接报废，把"告知"变成"毁掉证据"。
            #
            # 判损坏只留给**取值非法**这一档：三种合法 mode 之外的任何值
            # 都是明确的垃圾，没有第二种解释。
            _m = v.get("mode")
            if _m is not None and _m not in VALID_MODES:
                _corrupt_state(
                    f"files[{p!r}]['mode'] 非法：{_m!r}"
                    f"（合法取值：{'/'.join(VALID_MODES)}）", path)

    for b, t in (state.get("tasks") or {}).items():
        if not isinstance(t, dict):
            _corrupt_state(
                f"tasks[{b!r}] 应是对象，实际是 {type(t).__name__}", path)

    for c in (state.get("conflicts") or []):
        if not isinstance(c, str):
            _corrupt_state(
                f"conflicts 的元素应是字符串，实际是 {type(c).__name__}", path)

    return state


def _ftype(mode):
    """文件类型的人类可读名字，用于报错。"""
    for fn, name in ((stat.S_ISDIR, "目录"), (stat.S_ISFIFO, "FIFO/管道"),
                     (stat.S_ISLNK, "符号链接"), (stat.S_ISSOCK, "socket"),
                     (stat.S_ISCHR, "字符设备"), (stat.S_ISBLK, "块设备")):
        if fn(mode):
            return name
    return "非普通文件"


def _check_state_path(path):
    """基线路径是否可读的普通文件。返回 (存在, 不可用的原因)。

    用 stat + S_ISREG 判断，不能用「能不能打开」来试：
      · 目录  → open 抛 IsADirectoryError
      · FIFO  → open **一直阻塞**，没有任何输出，看起来像卡死
      · /dev/zero 之类的字符设备 → 能打开，但读不到头，内存被吃光
    这几种都不是「内容损坏」，而是路径本身就不该指向这里，必须提前挡掉。
    """
    try:
        st = os.stat(path)              # 跟随软链
    except FileNotFoundError:
        return False, None
    except NotADirectoryError:
        return True, "路径中的某一级不是目录"
    except OSError as e:
        return True, f"无法访问（{e.strerror or e}）"

    if stat.S_ISDIR(st.st_mode):
        return True, "是一个目录（应是文件）"
    if stat.S_ISFIFO(st.st_mode):
        return True, "是一个 FIFO/管道（按文件读会一直阻塞）"
    if not stat.S_ISREG(st.st_mode):
        return True, f"不是普通文件，而是{_ftype(st.st_mode)}"
    return True, None


def _load_state_strict():
    """读基线并校验结构。内容非法一律中止（见 validate_state 的说明）。"""
    exists, bad = _check_state_path(STATE_PATH)
    if not exists:
        if os.path.islink(STATE_PATH):
            # 软链指向的目标没了。os.stat 跟随软链所以走到这里。
            # 不当成「没有基线」默默过去：基线凭空消失会让人以为是脚本出错。
            # 也不中止 —— save_state 用 os.replace，会直接替换掉这个软链，
            # 所以 --init-baseline 能自愈，没必要挡住用户。
            print(f"  ⚠️  基线路径是悬空软链，目标不存在：{STATE_PATH}")
            print("     将按「没有基线」处理；跑 --init-baseline 即可重建。")
        return None
    if bad:
        _corrupt_state(bad)

    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except json.JSONDecodeError as e:
        _corrupt_state(f"不是合法 JSON（{e}）")
    except UnicodeDecodeError as e:
        # 二进制文件被误当成基线：json.load 在读的阶段就炸，不是 JSON 语法问题。
        _corrupt_state(f"不是 UTF-8 文本（{e}）")
    except OSError as e:
        # 权限不足、I/O 错误等。os.stat 已挡掉目录/FIFO，这里兜住其余。
        _corrupt_state(f"无法读取（{e.strerror or e}）")
    if state is None:
        # 文件内容是 JSON 的 null：json.load 成功但拿到 None。
        # 放过去就等于「没有基线」，会静默引导用户重建并丢掉旧基线。
        _corrupt_state("文件内容是 null（空基线应删掉文件，而不是写入 null）")
    state = validate_state(state)
    # 校验之后清掉「没有可用 sha」的记录：空 dict 能通过所有结构校验
    # （它是 dict，且没有键可供校验），于是长期存活并让文件级判据失效。
    # 必须在 load 这条实际路径上做 —— 不能只改 migrate_state。
    #
    # A26（P2）：这里原写「migrate_state 是死函数，改它不会被执行」
    # —— 那是**旧版的事实**，现已不成立：它在 9425 行有真实调用点
    # （`state, migrated = migrate_state(state, rstate, lmap=...)`）。
    #
    # 注释把一个会变的前提写死，而前提被后来的修复改变了 ——
    # 与 M42（N240 那条注释说 `_LAST_RUN_BLOCKED` 不在复位清单、
    # 实际被判据 5 补进去了）**同一形态**，判据 8 就是为它加的。
    #
    # 结论**没变**（load 路径上确实要做），变的是理由：
    # 不是"改 migrate_state 没用"，而是"两条路径都要覆盖"。
    dropped = drop_unusable_baseline_entries(state)
    if dropped:
        # M81（A85，P2）：v1→v2 升级时老基线没有 mode 字段**必然**触发
        # 这条，而它在分派之前 print —— `--status --json` 拿到的首行是它，
        # 整体不可解析而 rc=0。影响面比 A85 最初描述的更宽（A87 第二节：
        # 不必是 v1，任何触发基线清理/迁移的运行都命中）。
        #
        # N386（第一百二十四轮，中危）：判据原来是 `_json_active()`，
        # 只覆盖了 --json 一侧。而 --mine 与 --json 是**同一类**输出契约
        # （见 `_stream_active()` 的说明：消费者是 `| while read f`，
        # 混一行文案就被当成文件名）。实测 `--scope --mine` 的输出首两行
        # 正是本句与「基线已迁移到 v2」—— 被 `while read` 当成两个
        # 不存在的文件名送进 push_api.py。
        #
        # 同一份文件里 5618 那处（core.fileMode 环境提示）早已用
        # `_stream_active()` 且专门注释了理由，本处与 14200 / 4723
        # 没跟上 —— 正是「修一处 ≠ 修全部」的同族实例。
        if not _stream_active():
            print(f"  ! 已清理 {len(dropped)} 条无 sha 的基线记录"
              f"（{dropped[:5]}{'…' if len(dropped) > 5 else ''}）"
              f"：它们会让文件级校验失效，改按「基线未记录」处理")
    return state


def load_state(strict=True):
    """读基线。返回 None 表示「没有可用基线」。

    strict=False 只给 --init-baseline / --reset-baseline 用：那两条命令本来
    就要丢弃旧基线，旧基线合不合法无关紧要。此时坏文件一律按「没有基线」
    处理，用户直接重跑一次就能修好，不必先手工删文件。
    """
    if not strict:
        try:
            return _load_state_strict()
        except SystemExit:
            return None
    return _load_state_strict()


def drop_unusable_baseline_entries(state):
    """剔除基线里没有可用 sha 的记录（返回被剔除的路径列表）。

    空 dict `{}` 是 dict，能通过 migrate_state 的 isinstance 守卫
    **永久存活**；validate_state 只校验「有的键」，空 dict 没键可校验，
    也漏网。实测某仓库积累了 61 条这种记录 —— 它们每次推送都落进
    unrecorded 分支被拦下，而用户看到的只是「又被拦了」，
    看不出是脏数据（这正是「问题反复出现」的一部分）。

    剔除后这类文件按「基线未记录」处理 —— 那是 fail-closed（拦下），
    比「长期存活、且被 _local_matches_baseline 的 False 误读成
    『本地改过』从而判 overlap 放行」安全得多。

    N185：本注释曾断言「migrate_state 全文件 0 调用点」——那是旧版。
    现在它**有 1 个调用点**，位于子命令分派**之前**，即 --status /
    --pull / --prune 等只读命令也都会经过。

    断言过时比不写更有害：下一位维护者读到会以为改它没用，
    于是跳过真正该改的地方（load_state 的实际路径）。
    """
    files = state.get("files")
    if not isinstance(files, dict):
        return []
    # N411（第一百三十二轮，中危）：判据原本是
    #   `not (isinstance(v, dict) and bool(v.get("sha")))`
    # 而 **v1 基线存的就是裸 sha 字符串**（STATE_VERSION 上方注释：
    # 1=只记 sha；2=记 (mode,sha)）。裸字符串不是 dict → 被判"没有可用
    # sha" → **整批剔除**。
    #
    # 后果：migrate_state 里那行**专门为 v1 写的转换**
    #   `files[p] = {"mode": rstate.get(p, ("100644", v))[0], "sha": v}`
    # 在真实路径上执行 0 次 —— **是死代码**（实测：直接调 migrate_state
    # 能把 2 条 v1 转成 v2；走 load_state 后 files 为空，期望 2 实际 0）。
    #
    # 而 validate_state 里那条注释写着「v1/v2 两者**都由 migrate_state
    # 收敛**」—— 收敛对象在收敛之前就被清空了。
    #
    # 修法：sha 形态的裸字符串**留下来**交给 migrate_state 转（它有远端
    # rstate 能补 mode），这里只剔"转不出来"的（空 dict 等）。
    def _convertible(v):
        # v1 形态：40/64 位 hex 的裸字符串 —— migrate_state 能补 mode。
        if isinstance(v, str):
            return _is_sha(v)
        return False

    bad = [p for p, v in files.items()
           if not ((isinstance(v, dict) and bool(v.get("sha")))
                   or _convertible(v))]
    for p in bad:
        files.pop(p, None)
    return sorted(bad)


def _pushed_shas(entries, todo, lmap):
    """本次推送文件的 path → sha 记账。

    N267（第七十八轮，中危）：下面这行**曾经**成立，现在不成立了：

        git 通道：N193 跳过了 blob 上传，entries 为空 → …

    N207 已把 N193 撤销（见 `_build_entries` 上方 11219 一带的注释：
    「现在 blob 循环无条件执行，entries 始终完整，回退必然安全。
    代价：git 通道下仍会传一遍 blob（N193 的省请求收益暂不生效）。
    正确性优先于优化」）。所以**两个通道现在都走 `if entries:` 那一支**，
    entries 始终完整。

    保留 fallback 是因为它仍是 `entries` 万一为空时的兜底（防御，不是
    常规路径）。但原先那个 fallback **漏掉删除条目** —— 只取 `lmap` 里
    还存在的路径，而删除条目本地已不在 lmap 中：

        entries 完整（当前实际）: {'a.txt': 's1', 'gone.txt': None}
        entries 为空（旧 fallback）: {'a.txt': 's1'}           ← 少了 gone.txt

    已补成与上一支同口径（删除条目记为 None）。

    两者都是内容寻址、同一个 git 对象，sha 必然相同 —— 等价记账，
    不是近似。少了它，git 通道成功推送却零记账（N200/N207）。
    """
    if entries:
        return {e.get("path"): e.get("sha") for e in entries if e.get("path")}
    out = {}
    for p in (todo or []):
        ent = (lmap or {}).get(p)
        # 删除条目（本地已不在 lmap）记 None，与 `if entries` 那一支一致。
        out[p] = ent[1] if ent else None
    return out


def _rm_tmp(path):
    """删掉状态文件的 .tmp 残留。

    N183：序列化失败时 tmp 会留在磁盘上，权限 0644（chmod 在 dump 之后
    才执行）—— 里面是完整的仓库文件清单。删失败不报，它不是主流程。
    """
    try:
        os.unlink(path)
    except OSError:
        pass


def _scrub_url(url):
    """去掉 URL 里的 userinfo（`https://user:pass@host/…` → `https://***@host/…`）。

    M57（A65，**P1**）：`_git_push_url()` **有意**返回 origin 的原始 URL
    （N198 要保留 SSH / 自定义协议），而 origin 完全可能内嵌 token ——

        CI 里   git remote set-url origin \
                    https://x-access-token:${TOKEN}@github.com/o/r.git  ← 标准做法
        个人机  https://ghp_xxx@github.com/o/r.git                       ← 常见

    实证 `_url_matches` 对这两种形态**都返回 True**（即确实会被采纳）。

    这个 URL 有三条出路，**其中两条会把 token 明文打出去**：
      ① 进 `git fetch` / `git push` 的 argv —— 同机 `ps aux` 可见
      ② 进 SystemExit 消息（「git push {url} 失败」「…超时」）
      ③ `print("git 通道推送失败，回退 API：{_ge}")` —— **git 通道一旦
         失败就必发生**

    而脚本自己在两处明确禁止过这条路：
      · `_auth_config`：token 直接拼进 argv 时，同机任何用户 ps aux 都
        能看到它 —— 和「把 PAT 明文写进脚本」是同一类泄露
      · `_git_auth_env`：token 走**环境变量**而不是 `git -c
        http.extraHeader=...`，因为后者把 token 放在命令行参数里

    **防复发机制覆盖了 curl 通道与 `-c` 注入，没覆盖 origin URL 回传。**
    与 M42（N240 的前提被另一批修复改变）同族，但后果从「退出码错」
    升级为**凭证明文外泄**。

    ① 为什么不动（如实说明）：argv 里的 URL 是**认证所需** —— 非 GitHub
    主机（自建 GitLab / Gitea）确实依赖 URL 内嵌凭证，`_git_auth_env`
    注入的 `http.extraHeader` 对它们未必生效。剥掉 userinfo 会让这些
    场景推不上去，那是破坏性改动。
    """
    if not isinstance(url, str) or "@" not in url:
        return url
    import re as _re
    # scheme://userinfo@host → scheme://***@host
    m = _re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@]*)(@.*)$", url)
    if m:
        return m.group(1) + "***" + m.group(3)
    return url


def _b2s(b):
    """bytes → str。git 子进程走二进制管道（N206），
    文本结果在这里统一解码，避免每个调用点各写一遍。"""
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return b or ""


def _sanitize_state_paths(state):
    """把 state 里所有含非 UTF-8 字节的路径剔掉，返回被剔除的条数。

    M93（A98）：清洗原先是**逐个字段补** ——

        N85  补 local_seen
        N44  补「先判类型」（字符串也能被遍历，len 恒等 → 永远检测不到）
        N184 补 files 的 key 与 conflicts

    **每补一处就新漏一处**：`moved_away` / `pending_deletes` /
    `pending_removals` 三个同形态字段一直没覆盖。实测逐个注入含
    `\xff` 的路径，这三个全部：

        ★ SystemExit: 基线序列化失败：UnicodeEncodeError

    而 save_state 都在推送**之后**，于是「远端已生效、基线没写进去」，
    且**每次**推送都会崩（脏数据留在 state 里不会被自动清）。
    基线是「远端文件是否被他人改动」的唯一参照，写不进去就是文件级
    防护失效。

    最讽刺的一点：同一份 state 的 `files` 里那个路径**刚被清洗掉**
    并打了提示 —— 脚本知道它有问题，却在另一个字段里又带上了它。

    改成按**字段集合**整体兜底：新增含路径的字段只要登记进下面这张表，
    不再靠"谁想起来补谁"。
    """
    removed = 0
    # (字段名, 形态)：list=元素；dict=key；dictval=key 与 value 都是路径
    spec = (
        ("local_seen", "list"),
        ("conflicts", "list"),
        ("pending_deletes", "list"),
        ("pending_removals", "list"),
        ("files", "dict"),
        ("moved_away", "dictval"),
    )
    for name, kind in spec:
        v = state.get(name)
        if v is None:
            continue
        if kind == "list":
            if not isinstance(v, list):
                # N44：字符串也能被遍历 —— `len(str) == len(list(str))`
                # 恒成立，于是 `local_seen = "a.txt"` 被逐字符清洗后
                # 长度不变，**永远检测不到**。
                # local_seen 是「本地曾有过哪些路径」的缓存，丢了可由
                # sync_local_seen 重建；留着一份字符串会让它永久失效（N23）。
                state[name] = []
                removed += 1
                continue
            keep = [p for p in v
                    if isinstance(p, str) and _path_json_safe(p)]
            removed += len(v) - len(keep)
            state[name] = keep
        elif kind == "dict":
            if not isinstance(v, dict):
                continue
            bad = [k for k in v
                   if not (isinstance(k, str) and _path_json_safe(k))]
            for k in bad:
                v.pop(k, None)
            removed += len(bad)
        elif kind == "dictval":
            if not isinstance(v, dict):
                continue
            bad = [k for k, val in v.items()
                   if not (isinstance(k, str) and _path_json_safe(k))
                   or not (isinstance(val, str) and _path_json_safe(val))]
            for k in bad:
                v.pop(k, None)
            removed += len(bad)
    return removed


def save_state(state):
    """原子写：写 tmp 再 os.replace，避免写一半崩溃留下坏 JSON。"""
    # N85：--dry-run 此前只在子命令层做了白名单门禁（init/reset/pull…），
    # 而基线落盘发生在门禁**之后**的正常流程里 —— 预演照样写状态文件。
    # 实测 --dry-run --direct 还会把工作流永久改成"直推主干"：
    # 那是把一次预演变成了配置变更，且没有任何提示。
    #
    # 在**唯一出口**这里拦，而不是去每个调用点加 if dry：
    # save_state 有 16 个调用点，逐个加迟早漏一个（本轮其它条目
    # 反复出现的正是这个形态）。
    if _DRY_RUN:
        return True
    # 兜底：state 里的路径可能来自外部（sys.argv 以 surrogateescape 解码、
    # 外部模块维护的字段），带 surrogate 的路径会让 json.dump 抛
    # UnicodeEncodeError —— 而 save_state 都在推送**之后**，于是
    # 「远端已改、基线没写」，用户还会以为是磁盘问题。
    # 推送后的崩溃代价太高，不值得为了「只在一处过滤」的整洁去冒。
    #
    # M93（A98）：这里原先逐个字段补（N85 → N44 → N184），每补一处就
    # 新漏一处。现在统一交给 _sanitize_state_paths 按字段集合兜底。
    if _sanitize_state_paths(state):
        # M172（A155，低）：这句只说「已剔除」，**没说后果**。
        #
        # 被剔除的不只是基线条目，`local_seen` 里那一项也一起没了。而
        # `local_seen` 是「本地曾经拥有过哪些文件」的清单 —— 它的缺口
        # **不可回填**（需要它的时刻是文件已删的时刻，能补它的时刻是
        # 文件还在的时刻，两者互斥）。后果是：日后启用删除时，这些文件
        # 会被判成「远端独有」而**漏删**。
        #
        # 也就是说：一个文件名含非 UTF-8 字节的文件，从第一次运行起就
        # 永远进不了 local_seen，日后开 --delete 时不会被判成本地拥有过。
        # 方向是安全的（漏删而非误删），所以定低危 —— 但必须说出来，
        # 否则用户以为剔除了就没事了。
        print(f"  ! 基线里有含非 UTF-8 字节的路径，已剔除"
              f"（否则状态文件永远写不进去）")
        print("     这些路径也**不会**记入「本地曾拥有」清单，且该缺口"
              "不可回填：")
        print("     日后启用 --delete 时，它们会被判为「远端独有」而漏删"
              "（不会误删）。")
        print("     建议改名为 UTF-8 文件名后重新运行，让它们进得了清单。")
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        # 收紧到 0600：状态文件不含 token、不含文件内容，只有「仓库文件清单」
        # 和 remote 字段。私有仓库场景下，文件名清单本身也算信息泄露面。
        # os.replace 会沿用 tmp 的权限，所以要在 replace **之前** chmod。
        _ok, _got = _harden_mode(tmp, 0o600)
        _warn_mode("state_file", STATE_PATH, 0o600, _ok, _got)
        # N412（第一百三十二轮，中危）：`os.replace` 会**替换软链本身**，
        # 而不是写它指向的目标。读侧用 os.stat（跟随软链）所以一切正常；
        # 写侧替换掉软链之后：软链消失、软链**目标**停止更新停留在旧版、
        # 两份永久分叉，**全程无提示**。而用户刻意把基线软链到仓库外
        # （共享位置、备份盘、加密目录）正是合理用法。
        #
        # 原有注释只考虑了**悬空**软链（目标不存在 → 自愈，那条判断是
        # 对的，保留）。这里补另一半：软链**指向有效文件**时写到目标。
        # 与 N303（审计流水软链）同族，状态文件这一侧原先没修。
        _target = STATE_PATH
        if os.path.islink(STATE_PATH):
            _rp = os.path.realpath(STATE_PATH)
            if os.path.isfile(_rp):
                _target = _rp
        os.replace(tmp, _target)
    except OSError as e:
        # N183：tmp 会留在磁盘上，权限停在 0644（chmod 在 dump 之后执行），
        # 里面是完整的文件清单。两个失败分支都要清。
        _rm_tmp(tmp)
        # 这一步通常在推送**之后**，所以必须说清三件事，否则用户会做错反应：
        #  · 失败的是基线文件，不是要推的内容（远端可能已经生效）
        #  · 推送其实成功了，不用重推
        #  · 千万不要 --reset-baseline —— 那会丢掉「远端文件是否被他人改过」
        #    的参照，而这正是防覆盖的唯一依据。
        # 裸的 [Errno 28] 只会让人往磁盘/权限上查，看不出推送已生效。
        raise SystemExit(
            f"基线写入失败：{STATE_PATH}\n"
            f"  {e.strerror or e}（errno {e.errno}）\n"
            f"  ⚠ 注意：这一步在推送**之后**，远端改动可能已经生效。\n"
            f"  清理磁盘或修正权限后重新运行即可（基线会自然追上），"
            f"不要 --reset-baseline ——\n"
            f"  那会丢掉「远端文件是否被他人改过」的参照。"
        )
    except (TypeError, ValueError, RecursionError) as e:
        _rm_tmp(tmp)
        # 只兜 OSError 是不够的：json.dump 遇到**无法序列化的值**会抛
        # TypeError，而带 surrogate 的路径（非 UTF-8 文件名）会抛
        # UnicodeEncodeError —— 后者是 **ValueError 的子类**，
        # 不是 OSError，于是直接穿透成裸 traceback。
        #
        # 这处崩溃点在推送**之后**（save_state 都在最后），用户刚看到
        # “推送成功” 就撞上 traceback，且完全不知道远端已经生效。
        # 必须给出和 OSError 同样清楚的上下文。
        raise SystemExit(
            f"基线序列化失败：{type(e).__name__}: {e}\n"
            f"  文件：{STATE_PATH}\n"
            f"  ⚠ 注意：这一步在推送**之后**，远端改动可能已经生效。\n"
            f"  常见原因：文件名含非 UTF-8 字节（surrogate），"
            f"或状态里混入了不可序列化的值。\n"
            f"  不要 --reset-baseline —— 那会丢掉「远端文件是否被他人改过」的参照。"
        )


def migrate_state(state, rstate, lmap=None):
    """v1（只记 sha）→ v2（记 mode+sha），并补齐删除功能所需的 local_seen。

    v1 基线里没有 mode，迁移时取**远端当前**的 mode 填上——
    v1 的 sha 本就是远端 sha，远端 mode 是当时最接近事实的值。

    local_seen 是后加的字段（删除功能的第三道判据），老状态文件里没有。
    这里**显式**补齐而不是让它缺失然后靠「缺失即不删」降级：
      · 缺失时删除功能等于永久失效（老用户永远看不到删除提示），
        而用户不知道原因 —— 只会觉得这功能没做
      · 补齐后按「基线里有的路径本地都曾经有过」处理，这是**安全**的：
        init_baseline 记进基线的路径本来就来自本地同步，说它们「本地
        曾经有」与事实相符

    补不出来时（lmap 没传）保持缺失：宁可漏删，也不凭空造一份「曾经
    拥有过」的记录 —— 那会让「远端独有」的文件被当成删除候选。
    """
    # 守卫必须检查「有没有可用的 sha」，不能只检查 isinstance(v, dict)。
    #
    # 空 dict `{}` 也是 dict，于是它能通过守卫**永久存活**：
    #   · validate_state 只校验「有的键」，空 dict 没键可校验 → 漏网
    #   · 第一层判据是 `not base.get("sha")` → 恒为真 → 落 unrecorded
    #   · _local_matches_baseline 在基线缺失时返回 False（保守），
    #     而调用方把 False 一律读成「本地确实改过」→ 判 overlap → 放行
    # 于是这类文件既拦不住、又证明不了本地改过，静默推进主干。
    # 实测：61 条空记录就是这样活下来的。
    def _usable(v):
        return isinstance(v, dict) and bool(v.get("sha"))

    if (state.get("version") == STATE_VERSION
            and all(_usable(v) for v in state.get("files", {}).values())
            and isinstance(state.get("local_seen"), list)
            and bool(state.get("local_seen"))):
        # N43：短路条件从 `"local_seen" in state` 改成「是**非空列表**」。
        # 只判 `in` 的话，空列表和字符串都能通过短路 → 补齐逻辑不执行
        # → docstring 明写的动机（消灭「删除永久失效且用户不知原因」）
        # 在空列表/字符串形态下原样发生。
        return state, False

    files = {}
    dropped = []
    for p, v in state.get("files", {}).items():
        if _usable(v):
            files[p] = v
            continue
        if isinstance(v, dict):
            # 空 dict：没有 sha 可补，只能剔除。
            # 剔除后它落「基线未记录」分支 —— 那是 fail-closed（拦下），
            # 比「永久存活且被当成已改过放行」安全得多。
            dropped.append(p)
        else:
            files[p] = {"mode": rstate.get(p, ("100644", v))[0], "sha": v}
    if dropped:
        # M81-④（A85 复核，新增）：本处在**当前唯一调用点上不可达** ——
        # 唯一调用点 13296 走的是 load 路径，而 load 路径上
        # `drop_unusable_baseline_entries()`（3918）已经把空 dict 等
        # 剔除干净了；migrate_state 的 `_usable` 判据与它同口径，
        # 于是 `dropped` 恒为空。
        #
        #   实测：v1 基线（含空 dict + 非 sha 字符串）走 --status，
        #   只有 3918 那句「已清理 2 条」，本句**不出现**。
        #
        # 不删它：migrate_state 只要多一个不经 load 路径的调用点，
        # 它立刻复活，而它位于分派**之前** —— 复活的同时就会污染
        # `--status --json`（N99 判据：「分派前的任何输出都必须静音」）。
        #
        # 与 A3 那轮同形：不可达代码**不删、只把门控和注释改成事实** ——
        # 免得下一个人以为它"验证过可达"而放心依赖，或以为它活着而白改。
        # N386：同上，静音判据须覆盖 --mine 一侧（本处当前不可达，
        # 一旦多一个不经 load 路径的调用点就会复活并污染 --mine 管道）。
        if not _stream_active():
            print(f"  ! 清理了 {len(dropped)} 条无 sha 的基线记录"
                  f"（{dropped[:5]}{'…' if len(dropped) > 5 else ''}）："
                  f"它们会让文件级校验失效，已剔除并改按「基线未记录」处理")
    state["files"] = files
    state["version"] = STATE_VERSION
    state.setdefault("synced_commit", state.get("base_commit"))
    state.setdefault("conflicts", [])
    # local_seen：老状态文件没有它时才补。
    #
    # ⚠ 补的时候**不能取与本地当前的交集**。实测：本地删掉 gone.txt 后
    # 推送，迁移把 local_seen 缩成「基线 ∩ 本地当前」= 只剩 a.txt ——
    # gone.txt 被挤出「本地曾经有过」清单，于是删除检测永久失效：
    # 远端一直留着副本，而脚本再也不会提示。
    #
    # 用**基线全量**补才是准确的：init_baseline 记进基线的路径本来就来自
    # 本地同步（或远端独有），说它们「本地曾经有过」与事实相符。
    # 远端独有那一路的误判风险，由 pending_deletions 的「远端有」判据
    # 之外的**人工核查**兜住 —— 那份清单只报告、不自动删。
    # 空列表也算缺失：init_baseline 在插件关闭时会写成 []，
    # 只判 `not in` 的话补不进去，删除检测照样失效。
    # N43：`not state.get("local_seen")` 只覆盖了**空**形态。
    # 字符串 "a.txt" 是 truthy → 条件不成立 → 不补 → 删除功能照样永久
    # 失效，而这正是本字段要消灭的场景。判据必须是「类型是否有效」。
    _ls_now = state.get("local_seen")
    # 空列表**也要补**：它是 init_baseline 在插件关闭时写下的形态
    # （那时没有 sync_local_seen 可调），语义上等于「尚未初始化」，
    # 而不是「本地真的一个文件都没有过」。
    _ls_bad = (not isinstance(_ls_now, list)) or (
        isinstance(_ls_now, list) and not _ls_now)
    if _ls_bad and lmap is not None:
        # （原 `base = set(files)` 是旧实现"取基线全量"的残留，
        #  N277 改用 _locally_ever_owned 后已无引用 —— pyflakes 抓到。）
        # N277：上面这套论证（"基线全量才是准确的"）与 N238 的结论
        # **直接相反** —— 同一个字段、同一个文件、两条相反规则，
        # 各自都写着自己是"正确口径"。实测复现：
        #   本地只有 a.txt，基线含远端独有的 remote_only.txt
        #   → migrate 后 local_seen = ['a.txt', 'remote_only.txt']
        #   → plan_removals entries = 两者都删，skipped = []
        # 而 collect_removal_candidates 自己写着"不能对 seen 里没有的
        # 候选做基线兜底，会把别人的文件当成本地删掉的"。
        #
        # 但也不能简单翻回「基线 ∩ 本地当前」：N45 的互斥论证是成立的
        # —— 需要 local_seen 的时刻（文件已删）与能补它的时刻
        # （文件还在）互斥，只写 lmap 会让"迁移前已删除"的文件
        # 永远补不回来。
        #
        # 破局点在**本地侧**的证据源：本地 git 历史里删除过的路径。
        # 它恰好覆盖 N45 担心的那一类（已删），且天然不含远端独有文件。
        #
        # 口径改为：当前本地 ∪ 本地历史里删过的。取不到历史时退回
        # 当前本地（漏报），绝不退回基线全量（误删）。
        state["local_seen"] = sorted(
            p for p in _locally_ever_owned(
                files, lmap, state.get("remote_only"))
            if _path_json_safe(p))
        return state, True
        # 非 UTF-8 路径必须挡掉：带 surrogate 的文件名混进状态文件后，
        # save_state() 的 json.dump 会抛 UnicodeEncodeError —— 这条过滤
        # 已由上方 _locally_ever_owned(...) 分支的 _path_json_safe 承担，
        # 两处口径一致，不再保留第二份赋值。
    return state, True


def _locally_ever_owned(files, lmap, remote_only=None):
    """N381（第一百二十二轮，高危）：`remote_only` 是「远端独有、本地从未
    有过」的路径集合，由 init_baseline 持久化，这里**无条件扣除**。

    第 2 项证据源（本地 git 历史里删过的路径）在**克隆仓库**里不成立：
    本地 main 与 origin/main 是同一条提交链，别人在主干上的「加 x → 删 x」
    全在这条链上，于是被当成用户自己的删除 —— 判据 3 对 x.txt 恒真
    → **真删远端独有的文件**（用户从未拥有过）。

    git 历史**无法**区分这两者（都是"链上出现过删除"），所以只能靠
    init_baseline 当时记下的事实来排除，而不是事后猜。

    ── 本地曾经拥有过的路径 —— local_seen 的正确口径（N277）──

    两个极端都不能取：

    | 口径 | 后果 |
    |---|---|
    | 基线全量 | 含"远端独有、本地从未有过" → **真删别人的文件**（高危） |
    | 仅本地当前 | "迁移前已删除"的文件永远补不回来（N45 的互斥） |

    正确的本地侧证据源是两份的并集：

    1. **当前本地**（lmap + 实际存在的文件）
    2. **本地 git 历史里删除过的路径**（`--diff-filter=D`）

    第 2 项正是 N45 担心的那一类，且它天然不含远端独有文件 ——
    远端独有的文件从未出现在本地历史里。

    取不到历史时（非 git 仓库 / 浅克隆 / 命令失败）退回第 1 项，
    **绝不**退回基线全量：漏报可以靠人工核查补，误删不可逆。
    """
    base = set(files or {})
    _ro = {p for p in (remote_only or ()) if isinstance(p, str)}
    # `lmap` 在本文件的某些作用域里是**函数**（local_state_map 的别名），
    # 不是 dict。传入前逐个调用点核对成本高，这里按类型兜住：
    # 不是 dict/可迭代就当空（漏报），绝不因此抛异常打断迁移。
    _it = lmap if isinstance(lmap, (dict, set, list, tuple)) else {}
    cur = set(_it)
    # 未跟踪但确实存在的文件：lmap 取自索引，可能不含它们。
    try:
        cur |= {p for p in base if _local_exists(p)}
    except OSError:
        pass
    seen = set(cur)
    try:
        # N337（第一百〇七轮）：`--all` 是**所有 ref** —— 含别人推过的、
        # 合并过的、fetch 过但从未 checkout 的分支。于是：
        #   · 别人删过的文件   → 进 local_seen → 判"本地曾有过" → 真删
        #   · 合并冲突里被删的 → 同上
        #   · 远端分支上的删除 → 本脚本从未拥有过，照删
        #
        # 这正是 N238 警告的形态（"不能用基线兜底，会把别人的文件当成
        # 本地删掉的"），而 N277 只堵了 init_baseline 那一侧 ——
        # 历史侧是**同一个洞的第二面**。
        #
        # 只扫 HEAD：本地**当前这棵提交链**上删过的才算"曾拥有过"。
        # 代价是漏报（别的分支上删的文件不会被删到远端），
        # 而漏报的方向正是 N45 论证过的取舍：需要 local_seen 的时刻
        # （文件已删）与能补它的时刻（文件还在）互斥，补不回来就漏着，
        # 误删不可逆。
        out = git("log", "--diff-filter=D", "--name-only",
                  "--pretty=format:")
        for ln in (out or "").splitlines():
            p = ln.strip()
            if p and p in base:
                seen.add(p)
    except BaseException:
        pass          # 取不到历史 → 退回当前本地（漏报，不误删）
    # N381：远端独有的路径无论从哪条证据源来都要排除 —— 它意味着
    # "本地从未拥有过"，判据 3 对它必须恒假。
    return seen - _ro


def _path_json_safe(p):
    """路径能否安全地进 JSON（及 API 请求体）。

    N47：本函数与 delete_rename_support._encodable 是两份重复实现。
    保留两份是有意的（插件必须自包含、不能依赖宿主），但**必须同源**：
    两边的判据一旦漂移，会出现「push_api 认为安全、插件认为不安全」
    的错配。因此这里显式注明另一份的位置与必须同步的约定。

    N253：原先这里挂着**第二个**字符串字面量
    「路径能否安全写进 JSON（= 能否 UTF-8 编码）。」。
    它不是 docstring（Python 只认第一个），每次调用构造即弃，
    且内容与第一份首行重复 —— 是 ast 扫描能发现的死字面量。
    保留的信息已并进上面：能否安全 = 能否 UTF-8 编码。
    """
    try:
        p.encode("utf-8")
        return True
    except (UnicodeEncodeError, AttributeError):
        return False


# _CHANGES_CACHE = None  # → Runtime 实例属性（见 Runtime 段）


def _detect_deletions():
    """从 git status 取「已删除」路径（未 commit 的那一路）。

    必须单独走 git status：local_state_map() 基于 git 索引，而**未 commit
    的删除仍留在索引里**（实测 `rm a.txt` 后 `git ls-files` 照样列出它），
    所以「基线有 & lmap 无」这一路抓不到它。反过来，git status 的 "D"
    抓不到已 commit 的删除（那时工作区干净，一个字都没有）。

    两路必须并集，少一路就漏一种：
      · 只有 git status "D"  → 推送成功后脚本自动补 commit，删除信号丢失
      · 只有「lmap 无」      → 未 commit 的删除还在索引里，压根看不见
    """
    # git() 返回的是**字符串**（已 rstrip 掉末尾换行），不是 subprocess
    # 结果 —— 写 .stdout 会抛 AttributeError。这里若用宽泛 except 兜住，
    # 症状是「删除功能静默不工作」，排查方向完全错。
    # N58：必须加 --no-renames，与 detect_changes() 保持同一口径。
    # 不加时 `git mv old new && rm new` 的 -z 输出是 "RD new\0old"：
    #   · xy="RD" 含 D → 命中，追加的是 **new.txt（新路径）**
    #   · 旧路径 old.txt 由 "R" 分支消费，但 D 先命中，永远走不到
    #   · 下一轮 item="old.txt"，xy="ol" 不含 D/R → 整条静默丢弃
    # 于是新路径成了幻影删除项（N57 的假消息来源之一），而两个函数对
    # 「哪个路径才是删除」的判断互相矛盾。
    # 加 --no-renames 后变成 "AD new" + "D  old"，两路各自明确。
    # N64：必须 fatal=True，与 detect_changes / local_state_map 同一口径。
    # git 失败时返回空字符串 → 「没有删除」→ 删除信号静默丢失。
    # 而丢失删除信号正是本模块存在的唯一理由要防的事（docstring 自述
    # 「删除信号丢失是灾难」）：文件在远端删不掉，用户以为删了。
    out = git("status", "--porcelain", "-z", "--no-renames", fatal=True)
    if not out:
        return []
    # 末尾换行被 rstrip 掉了，但 NUL 分隔的中间字段不受影响；
    # 唯一的边界是「输出恰好以空字段结尾」，split("\0") 会多出一个空串，
    # 下面的 len(item) < 3 检查会跳过它。
    fields = out.split("\0")
    dels, dels_skipped, i = [], [], 0
    while i < len(fields):
        item = fields[i]
        i += 1
        if not item or len(item) < 3:
            continue
        xy, path = item[:2], item[3:]
        if not path:
            continue
        # N56：删除路径此前是**唯一**不过起飞前消毒的路径类别。
        # 待推文件、--force-file、pull 路径都过了 safe_rel + UTF-8 检查，
        # 唯独删除路径没有 —— 而 N8（deleted 混入非 UTF-8 导致请求体编码
        # 崩溃）与 N22（added 侧同类）的共同根因就在这一处。
        #
        # 为什么必须在这里挡而不是等模块内部过滤：
        # 含 surrogate 的路径要走到 json.dumps 才炸，而那时分支已在远端
        # 建好，崩完留下没人管的僵尸分支 —— 消毒必须在建分支之前完成。
        # 删除路径恰恰是在建分支**之后**才算出来的，所以补在这道出口上。
        if "D" not in xy and "R" not in xy and "C" not in xy:
            continue
        _r = safe_rel(path)
        if _r is None:
            # 越界路径（../ 或绝对）：不进删除清单，也不静默丢弃
            dels_skipped.append(path)
            continue
        if not _path_json_safe(_r):
            # 非 UTF-8：同样跳过并计数，调用方会打印
            dels_skipped.append(path)
            continue
        if "D" in xy:
            dels.append(_r)
        elif "R" in xy or "C" in xy:
            # 重命名在 -z 下是 "R  新路径\0旧路径"，旧路径要吃掉
            if i < len(fields):
                i += 1
    # N56：把被消毒挡掉的路径一并返回。原先静默丢弃 ——
    # 用户删了一个含非 UTF-8 字节名的文件，会看到「没有待推送的改动」，
    # 而实际上有一个删除被永久忽略了（且没有任何解释）。
    _detect_deletions.skipped = dels_skipped
    return dels


def _detect_deletions_skipped():
    """上一次 _detect_deletions 被消毒挡掉的路径（N56）。

    挂成函数属性而不是改返回值：改动返回契约会波及所有调用点，
    而这是一条**告知性**信息——它不参与任何判据，只用于打印。
    """
    return list(getattr(_detect_deletions, "skipped", []) or [])


_SUBMODULE_CACHE = None


def _dry_preview_removals(state, head_sha, ignored_files, files,
                          opts, pending_dels, allowed=None, lmap=None):
    """--dry-run 的删除预览（N165）。

    原先这段逻辑内联在「PR + 新分支 + dry」那一个分支里，
    于是 direct 工作流与 --hold（复用已有分支）下**完全不展示** ——
    而 dry-run 的意义恰恰是"任何工作流都能先看一眼"。

    抽成函数后，所有 dry 出口调同一个函数：口径一致、改一处即全改。
    内联副本正是这批审查报告反复批评的形态（同一份逻辑多处）。
    """
    # N180：守卫顺序错了 —— `_plan_removals` 在删除关闭时是 None
    #（_bind_removals 把它整个置空），于是第一行就 return，
    # 下面"删除功能已关闭"的提示**永远走不到**（死代码），
    # --dry-run 在关闭态下不再提示任何待删除项。
    #
    # 关闭态恰恰是最该提示的场景：用户删了文件、远端还留着，
    # 预演要告诉他"这些不会自动删"。
    if not pending_dels:
        return
    if _deletes_disabled():
        print(f"\n  · [dry-run] 有 {len(pending_dels)} 个删除待落实，"
              f"但删除功能已关闭，本次不会删除任何远端文件。")
        return
    if not _plan_removals:
        return
    try:
        _rm_dry = _plan_removals(
            state, remote_state_map(head_sha),
            local_state_map(), deleted=pending_dels,
            exists=_local_exists,
            gitignored=(lambda p: p in ignored_files),
            submodules={p for p, (m, _s) in (state.get("files") or {}).items()
                        if _is_submodule_path(p)},
            # 口径说明：此处在建分支**之前**，最终的待推集合 todo
            # 尚未算出（它在 FileBaselineGuard 之后才有）。
            #
            # N248：上面这段是 N182 之前的**过时注释**。N182 之后
            # 唯一调用点传的是 `sorted(set(todo) | set(extra))`
            #（已过完 Guard 的真正会推的集合），不再是候选集 files。
            #
            # 仍存在的差异只有一处：extra（分支上要补齐的路径）。
            # 所以口径不是"略宽松"，而是**一致**（todo 同源）。
            # 注释与实现不符比没注释更糟 —— 它会让人以为预览不可信。
            # 比不传（把改名全判成真删除、误报熔断）更接近事实，
            # 但要如实告知，不能假装完全等价。
            will_push=set(files),
            # N169：**原注释已过时（N597 / A156，低）** —— 它用现在时写
            #「正式路径传了，预览不传」，而三处（含它自己）**全部已传**。
            # 保留原文字并标注，是因为这一族（N14 / M84 / N597）已经反复
            # 出现"注释说已修实际未修"和"注释说未修实际已修"两种**反向**
            # 形态；留着过时原文比删掉更不容易被后人照着"补"回去。
            #
            # N11：`--force-overwrite` 的语义是「覆盖远端」（放行文件级
            # 校验），它**不是**「允许删除」。两者混用会让用户为了推一个
            # 被误报的文件而加 --force-overwrite，顺带授权了本次全部删除
            # —— 而删除比覆盖更不可逆（覆盖的内容还在旧 commit 里）。
            # 作者在 ignore_pending 上已经识别过这个模式（那里引入了独立
            # 开关），删除这一处此前没有对应的独立开关，现在补上。
            #
            # N595 / M174：`bool(force) or force_delete` 里 `bool(force)`
            # 在左边 → `--force-overwrite` **单独**就放行全部删除判据
            #（不需要 --delete）。改走 `_deletes_forced()`，只认专用开关。
            force=_deletes_forced(opts),
            # M83（A86，P2）：`plan_removals` 全文 3 个调用点，
            # 原先**只有正式执行**传了 `allowed`（N370），
            # 而本处（dry 预览）与预检 `_rm_pre` 都不传。
            #
            # 不传 allowed 就等于**没过滤**：模块内部会把 committed
            # 补回来（16312 的过滤必须放在并集之后，放前面等于没过滤）。
            # 实测（点名推 a.txt、b.txt 在范围外）：
            #     传 allowed={'a.txt'}  entries=['a.txt']
            #     不传                  entries=['a.txt','b.txt']  ★
            #
            # 后果：dry-run 是"先看一眼再决定是否 --yes"的入口，
            # 预览说会删 b.txt、真跑却不删 —— 预演的价值全在
            #「结论与真跑一致」（本文件 14576 那句注释），
            # 不一致的预览比没有预览更糟：它给了错误的安全感。
            #
            # 与 N268 修掉的 force 完全同形态（那时是"两处传、一处不传"，
            # 现在是"一处传、两处不传"）。
            allowed=allowed)
        print("\n  · [dry-run] 删除预览：")
        # M23（A20 / A24）：紧邻的注释刚把口径改成
        #「不是'略宽松'，而是**一致**（todo 同源）」，
        # 而这行**输出文案**还写着"略宽松"。
        #
        # 它修了「注释 vs 实现」，漏了「注释 vs 输出文案」——
        # 而**输出文案是用户唯一看得到的东西**。
        # 该注释自己写着"注释与实现不符比没注释更糟"，
        # 那么"输出与注释不符"就是最糟：用户读到的是错的那一版。
        print("    （预演在建分支前进行，删除判断与正式推送同源；"
              "正式推送会按最终待推集合再判一次）")
        # N249：正式路径已接 ui（N105），dry 这条没接 ——
        # 于是 --json 模式收集不到删除预览的行。
        # 当前 --json 只对 --status 有效（N149），影响有限，
        # 但等 N149 修好后这里会自动变成真缺口。先接上。
        # N521：与正式路径同源 —— `_NON_UTF8_DROPPED` 在 dry 出口同样是
        # 空的（sync_local_seen 还没跑过），恒 0 让这句提示永不打印。
        # 有 lmap 时当场统计，没有就退回原值（不无中生有）。
        _n_utf8 = len(_NON_UTF8_DROPPED)
        if not _n_utf8 and lmap is not None:
            _n_utf8 = sum(1 for _p in lmap if not _encodable(_p))
        _report_removals(_rm_dry, dry_run=True, ui=get_ui(),
                       skipped_non_utf8=_n_utf8)
    except Exception as _e:
        print(f"  ! 删除预览失败：{type(_e).__name__}: {_e}")


def _is_submodule_path(rel):
    """判断路径是否是 submodule（gitlink，mode 160000）。

    为什么必须单独查：
      · remote_state_map() 只收 `type == "blob"` 的条目，而 GitHub tree API
        里 submodule 的 type 是 "commit" → 被过滤，rstate 里**恒取不到**
      · 于是删除模块里「远端已被他人改动」那条判据对 submodule 永久短路
        放行（rsha 恒为 None） —— 而这条判据正是它存在的唯一目的（N1）

    判据用 git 索引里的 mode（160000），**不用基线里的 mode**：
    基线里的 mode 经过 VALID_MODES 白名单，160000 会被回退成 100644
    （见 local_state_map），查基线查不出来。

    索引里查不到的（比如本地已删、或远端独有）另用 .gitmodules 兜一层。
    """
    global _SUBMODULE_CACHE
    # 缓存键要带 ROOT：只存集合的话，切到另一个仓库（测试 monkey-patch
    # ROOT、或一次跑多个仓库）会拿上一个仓库的结果回答，实测串味。
    if _SUBMODULE_CACHE is not None and _SUBMODULE_CACHE[0] == ROOT:
        return rel in _SUBMODULE_CACHE[1]
    paths = set()
    ok = True
    try:
        # C1（A23 降级 P3）：主流路径都用了 `-z`（NUL 分隔），
        # 只剩这一处用换行分隔 —— 路径里含换行（文件名允许）
        # 时会被切成两行，mode 解析拿到的是残缺串 → 漏判 submodule。
        # 而漏判的方向是**放行**（N1 闸门短路），不是拦下。
        out = git("ls-files", "-s", "-z")
        for line in (out or "").split("\0"):
            if not line.strip():
                continue
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) >= 1 and parts[0] == "160000" and path:
                paths.add(path)
    except BaseException:
        # git 读失败（仓库锁 / 权限 / 中断）时**不能**当作「没有 submodule」：
        # 那会让 N1 的闸门静默失效 —— 而这正是它存在的唯一目的。
        # 不缓存空结果，下次重新查。
        ok = False
    # .gitmodules 兜底：本地已删 / 远端独有的 submodule 不在索引里
    try:
        gm = os.path.join(ROOT, ".gitmodules")
        if os.path.exists(gm):
            import configparser
            cp = configparser.ConfigParser()
            cp.read(gm, encoding="utf-8", errors="surrogateescape")
            for sec in cp.sections():
                pth = cp.get(sec, "path", fallback="").strip()
                if pth:
                    paths.add(pth)
    except BaseException:
        ok = ok and False
    if ok:
        _SUBMODULE_CACHE = (ROOT, paths)
    return rel in paths


def _deletes_disabled():
    """删除功能的总开关。**默认关闭**。

    删除是脚本里唯一不可逆的远端操作，而它的误判成本不对称：
      · 漏删 → 远端残留一个文件，用户下次还能删，可恢复
      · 误删 → 删掉的可能正是别人刚加的内容，虽能从 commit 历史捞回，
               但用户通常不会第一时间发现

    所以默认不删，只**精确提示**「你本地删了哪些、远端还留着、怎么开」。
    用户看到提示后自己决定，比脚本替他决定安全。

    开启方式（优先级从高到低）：
      1. --no-delete           强制关闭，压过一切（CI 里兜底用）
      2. --delete              单次开启
      3. PUSH_API_ENABLE_DELETE=1  环境级开启（常开场景）
      4. 以上都没有 → 关闭
    """
    if _NO_DELETE:
        return True
    if _ENABLE_DELETE:
        return False
    v = (os.environ.get("PUSH_API_ENABLE_DELETE") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return False                       # 明确开启
    # N42：原来只认这四个字面量，于是 `PUSH_API_ENABLE_DELETE=2` 被判成
    # **关闭** —— 而 2 在语义上显然是「要开」（它非零非空）。
    # 更糟的是它**静默**：用户设了值、以为开了，删除却一路被丢弃，
    # 且提示里说「已开启」的路径也不会走到。
    # 非零数字一律按开启处理；空 / 0 / false / no / off 才关。
    if v in ("", "0", "false", "no", "off"):
        return True
    try:
        return float(v) == 0.0             # 其他数字：非零即开
    except ValueError:
        return True                        # 认不出的按关闭（fail-safe）


def _delete_how_to_enable():
    """关闭时告诉用户怎么开。给**能直接复制的命令**，不给抽象说明。"""
    if _NO_DELETE:
        # N41 当年加的括号写着「若由环境变量置位，请取消该环境变量」——
        # 而 `_NO_DELETE` 的置位点只有命令行 --no-delete 一处
        # （对照：PUSH_API_ENABLE_DELETE 才真有环境变量）。那句括号描述
        # 的状态**不可达**，用户会去翻 CI 的环境变量列表，而真因就写在
        # 命令行上（M196，A166，低危）。
        return "去掉命令行上的 --no-delete 参数"
    # M195（A166，低危）：原来把换行与第二行的缩进**写死在返回值里**
    # （"\n     长期开启：…"），而六个调用点各自再加一层前缀 —— 实测
    # 前导空格数 6 与 5，**差 1 格**；前缀为 0 那处更离谱，第二行比
    # 第一行多缩进 5 格，看着像第一行的续行说明而不是并列的两条命令。
    #
    # 这个函数的 docstring 第一句就是「给能直接复制的命令」—— 两条命令
    # 没对齐，复制的观感是坏的。换行与缩进交给 _print_how_to_enable()
    # 统一排版，本函数只管内容。
    return ("本次开启：python3 push_api.py --delete -m \"...\"\n"
            "长期开启：export PUSH_API_ENABLE_DELETE=1")


def _print_how_to_enable(indent=6):
    """按统一缩进逐行打印开启方式（M195）。

    缩进由**调用方**给，不再由被返回的字符串写死 —— 六个调用点五种
    对齐的根源就是写死。
    """
    _pad = " " * int(indent)
    # N624（第二百〇六轮，中危）：这里原先是裸 print —— 于是 `report_pending
    # _deletes` 改走 Ui 之后，出口清单在 --json 下**仍会**把「本次开启 /
    # 长期开启」两行打到 stdout，落在 JSON 之前（实测判据 N624.2 红）。
    # 修一半比不修更难查：看起来改了、机器仍读不到。五个调用点一并收口。
    _ui = get_ui()
    for _line in _delete_how_to_enable().split("\n"):
        _ui.line(_pad + _line)


def _offer_delete_now(paths, dry=False):
    """关闭态下就地问一句「是否删除这 N 个」，答 y 则**仅本次运行**开启。

    为什么必须问：清单原本只给「怎么开」，用户得自己重跑一遍命令。
    而按清单自己的设计（一次运行只打一次、且在最后），扫完之后没有人
    会去逐个手工删 —— 功能退化成扫描器：每次运行打同一份、每次都不
    解决，远端残留永久化，提示本身也变成噪音被无视。

    两条刻意**不**豁免：
      · `-y/--yes`：它的语义是「跳过**已声明意图**的确认」。用户这次
        并没说要删，凭空替他创造删除意图，正是 15613 那道独立删除确认
        存在的原因（那一处的前提是用户已经开了删除）。
      · `--no-delete`：文档写明它「压过一切」，再问等于违背它。

    非交互环境由 safe_input 兜住（返回 "" → 按否），CI / 管道不受影响。

    返回 True 表示本次应删除（已置 _ENABLE_DELETE）。
    """
    if not paths or dry or _NO_DELETE:
        return False
    _ans = safe_input(
        f"\n  是否删除这 {len(paths)} 个远端副本？"
        f"此操作不可逆，输入 y 继续 (y/N) ").strip().lower()
    if _ans != "y":
        # M200（A168，低危）：这句说「见上方开启方式」，而混合场景
        # （有改动同时有删除）在提问这个时刻，上方**一处都没有** ——
        # _delete_how_to_enable() 的打印点要么在「纯删除」分支内、
        # 要么在推送**之后**。指向末尾那份待删除清单才是准的。
        print("  本次不删除（远端副本保留，"
              "开启方式见末尾的待删除清单）。")
        return False
    global _ENABLE_DELETE
    _ENABLE_DELETE = True
    # M199（A168，中危）：只翻开关**不够** —— 真正执行删除的实现是
    # **绑定期**决定的：_bind_removals() 在关闭态把 _plan_removals 置成
    # None，而它只在 parse_args 之后被调用过一次（那时删除还是关的）。
    # 于是答 y 之后的真实状态是「开关亮着、实现还是 None」：本次运行里
    # 「✓ 本次将删除 N 个」与「删除/重命名支持未启用，本次不会删除任何
    # 远端文件」同屏出现，而事实是一个都没删。
    # _bind_removals 的 docstring 自己写着它**必须可重入** —— 缺的正是
    # 这一个调用点。
    _bind_removals()
    print(f"  ✓ 本次将删除 {len(paths)} 个（仅本次运行生效）。")
    return True


def reset_per_run_globals():
    """复位所有「单次运行」级别的全局标志。

    真实 CLI 每次都是新进程，天然不串档。但**测试在同一进程里跑多个
    用例**：前一个用例的 --dry-run / --delete 会残留到后一个。
    实测：某套件里跑过 --dry-run 之后，save_state 的 dry 闸门对后续
    **直接调用**静默生效，于是 surrogate 用例拿到「未抛异常」——
    表现为测试红，而根因是全局状态串味。

    与其在每个用例里逐个复位（迟早漏），不如一个显式函数列全。
    这也顺带自文档化了「哪些全局是 per-run 的」。
    """
    global _DRY_RUN, _NO_DELETE, _ENABLE_DELETE
    global _REWIND_NO_BASELINE_WARNED, _AUDIT_WARNED
    global _MOVE_DETECT_UNAVAILABLE_WARNED
    # N569：`_REWIND_KIND` 是「本次判定出的回退类别」，remedy 侧按它分流。
    # 不复位的话，同进程里前一个用例判出的 "switched" 会残留到后一个
    # —— 于是后一个真回退的用例拿到 `git checkout <别的分支>` 那条 remedy，
    # 与 why 说「回退」自相矛盾（正是 N569 实证①的形态，只是由串味触发）。
    global _REWIND_KIND
    _DRY_RUN = False
    _NO_DELETE = False
    _ENABLE_DELETE = False
    _REWIND_NO_BASELINE_WARNED = False
    _REWIND_KIND = None
    _AUDIT_WARNED = False
    # M133：与上面两条同族的一次性告知标志。不复位的话，同进程里
    # 第二个用例就再也打不出这条提示了（判据 5 那一族）。
    _MOVE_DETECT_UNAVAILABLE_WARNED = False
    # N150：`Ui.suppressed` 挂在 Ui **实例**上，而 _UI 是模块级单例。
    # 置位后无人清除 —— 同进程第二次 --json 运行的输出**完全空白**
    #（events 收了但不落地）。
    # 这与本函数自述的设计意图直接冲突："与其在每个用例里逐个复位
    #（迟早漏），不如一个显式函数列全"。挂在实例上的状态也要列出。
    global _JSON_EMITTED, _RUN_PENDING_DELETES, _RUN_DELETED_PATHS
    # ── per-run 复位四条（A25 第一档 M10/M15/M25/M32）──────────────
    # 四条全部经端到端实证泄漏：
    #     _RUN_ID           第1次 == 第2次 == 20260920-091517-de0327
    #     _DRY_THIS_RUN     reset 后 = True
    #     safe_input._warned reset 后 = True
    #     _LAST_RUN_BLOCKED  reset 后 = 'stale_local'
    #
    # 稳定模式很说明问题：已修的 4 条（N204/N168/N163/N143）**都在复位
    # 函数里留了详细注释**；未修的这 4 条**恰好都没留**。
    # 说明这份清单是「逐条补」的、不是「按规则扫」的 ——
    # 每新增一个 per-run 全局就有约一半概率漏掉。
    #
    # 所以补这 4 条的同时，把「扫描模块级 + 被 global 声明并赋值、
    # 断言都在这里复位」这条判据加进了 check_invariants.py（判据 5）。
    #
    # 刻意**不放在本文件里**：push_api.py 里再放一份自检，会变成
    # 第二份零调用点的机制 —— 正是判据 3 要抓的形态
    # （实测它自己就被报成了「模块级零调用函数」）。
    # 检查器与层检查一样放在 run_all.py 里跑，才有触发点。
    global _RUN_ID, _DRY_THIS_RUN, _LAST_RUN_BLOCKED
    # N362：取消标志与 _LAST_RUN_BLOCKED 完全同族 —— 判据在复位之后才读，
    # 所以它**必须**在复位清单里（否则跨运行串味），同时必须"先取值 →
    # 再复位 → 最后写回"。M42 踩过一次：直接把它挪出清单会让上一次的
    # 结果污染下一次成功运行，退出码假报。
    global _LAST_RUN_CANCELLED
    global _LEGACY_STATE_ACTIVE      # N324：由 resolve_config 每次重算
    # 不复位的话，同进程第二次运行会"继承"上一次的仓库依据 ——
    # 第二次既没有 --repo 也没有 remote，却被认为已指定，
    # 于是把内容推到编译期那个默认仓库。方向正是本函数要防的那一侧。
    global _REPO_EXPLICIT
    # N362：取消标志与 _LAST_RUN_BLOCKED 完全同族 —— 判据在复位之后才读，
    # 所以它**必须**在复位清单里（否则跨运行串味），同时必须"先取值 →
    # 再复位 → 最后写回"。M42 踩过一次：直接把它挪出清单会让上一次的
    # 结果污染下一次成功运行，退出码假报。
    global _NOTHING_TO_PUSH
    # M126：终止信号不复位 → 同进程里先跑一次"被 SIGTERM 打断"的用例，
    # 之后一次**正常 Ctrl-C** 的用例也会报成"被 SIGTERM 终止"，
    # 退出码变 143。与 _LAST_RUN_BLOCKED 完全同族。
    global _LAST_TERM_SIGNAL
    # A6-J2：同上。不复位 → 同进程里先跑一次 --new-task 的用例，
    # 之后一次**没有** --new-task 的推送也会把上次的旧分支摘掉。
    global _ORPHANED_TASK_OLD
    _RUN_ID = None
    _DRY_THIS_RUN = False
    _LAST_RUN_BLOCKED = None
    _LAST_RUN_CANCELLED = False
    _NOTHING_TO_PUSH = False
    _LAST_TERM_SIGNAL = 0
    _ORPHANED_TASK_OLD = None
    _LEGACY_STATE_ACTIVE = None
    _REPO_EXPLICIT = False
    # M91：限流熔断标志同上。不复位 → 同进程里先跑一次"被限流"的用例，
    # 之后**正常**的用例也被判成已限流、剩余文件全部计入 failed。
    global _RATE_LIMITED
    _RATE_LIMITED = False
    # N383：本次已推到的 ref 与"是否已记过 push_fail"。不复位 →
    # 同进程里第二次运行会把上一次的分支名记进这一次的失败流水里，
    # 事后追溯看到的是错的分支（比没有更糟——它会把人引向别处）。
    global _LAST_TARGET_REF, _PUSH_FAIL_AUDITED
    _LAST_TARGET_REF = None
    _PUSH_FAIL_AUDITED = False
    # M44（A54）：不复位 → 同进程里跑过一次含非 UTF-8 路径的用例，
    # 之后**不含**的用例也会打印"N 个非 UTF-8 路径已排除"，
    # 而那批路径根本不属于这次运行。
    global _NON_UTF8_DROPPED
    _NON_UTF8_DROPPED = set()
    # _warned 挂在函数对象上（同 N143 修的 _detect_deletions.skipped）
    try:
        safe_input._warned = False
    except BaseException:
        pass
    # 第四次同形态（自查器抓到的）：L1 给 _conflict_path 加的"只提示一次"
    # 标志。它挂在函数对象上，逐个列模块级全局时**必然漏** ——
    # N143 / N163 / M10 都是它。
    #
    # 漏复位的后果：同进程跑第二次时"冲突目录不可用"不再提示，
    # 而那正是唯一告知用户"副本没写成"的渠道。
    try:
        _conflict_path._warned = False
    except BaseException:
        pass
    # 第五次同形态（N364）：_local_exists 的"只提示一次"标志。
    # 漏复位的后果：同进程跑第二次时不再提示，而那正是唯一告知用户
    # "这个文件查不到、所以没删"的渠道 —— 用户会以为它真被删了。
    try:
        _local_exists._warned = False
    except BaseException:
        pass
    # N454：同上，逐路径的「查不到」登记与 errno 去重集合也要复位，
    # 否则跨运行串味 —— 第二次运行时 N456 的 skipped 原因会沿用上次的。
    try:
        _LOCAL_EXISTS_UNKNOWN.clear()
        _LOCAL_EXISTS_WARNED.clear()
    except BaseException:
        pass
    # M59（A67）：**第三类** per-run 状态 —— 模块级容器。
    #
    # 判据 5 扫的是「模块级 + 在函数里被 `global` 声明**并赋值**」。
    # 而容器是**就地修改**（`_X.add()` / `.append()`），根本不需要
    # `global` 声明 —— 于是判据 5 对它们输出「✓ 无新增遗漏」，**看不见**。
    #
    # 实测后果（同进程第二次运行）：
    #     第 1 次  ⚠️ 无法收紧到 0o600（实际 0o777）…   chmod 调用 1 次
    #     第 2 次  不再提示                              chmod 是否再次被调用: False
    #
    # 两道门叠加：`_HARDENED` 让 `_harden_mode` 根本不执行，
    # `_HARDEN_WARNED` 让即使执行了也不打印。
    # 而 `_harden_mode` 的 docstring 自己写着这条提示的分量：
    #   「不验证的话，脚本会一直宣称一个它并没有提供的保护，
    #     而用户无从察觉 —— 这正是本工具最该避免的『静默失败』。」
    #
    # 即：同进程第 2 次运行时，**这个唯一告知渠道没了**。
    #
    # `_ATTR_CHECK_FAILED` 后果最轻（只累积增长，`[-1]` 总是对的），
    # 但它是 N219 新引入的、且在**缓存**语义上属于"按仓库"状态 ——
    # 跨仓库残留会让 `_ATTR_CHECK_FAILED[-1]` 读到**上一个仓库**的原因。
    #
    # 用 `.clear()` 而不是重新赋值：重新赋值会让别处 `from ... import`
    # 或已持有的引用指向旧容器，等于没清。就地清空对所有持有者同时生效。
    for _c in (_HARDENED, _HARDEN_WARNED, _ATTR_CHECK_FAILED,
               _JSON_PENDING_WARNINGS, _KNOWN_ARTIFACT_NAMES):
        try:
            _c.clear()
        except BaseException:
            pass
    global _BLOCKED_BY_GUARD, _AUDIT_GUARD_DONE
    global _DISPATCH_REACHED      # M80：finally 的"不支持 --json"要看它
    global _AUTH_PROBING          # N416：_auth_hint 的重入保护
    global _DRY_RUN_ONLY          # M104：同上，per-run
    global _JSON_UNSUPPORTED_WARNED  # M80-2（A84）：本运行已说过"不支持"
    _DRY_RUN_ONLY = False
    _JSON_UNSUPPORTED_WARNED = False
    _JSON_EMITTED = False
    _RUN_PENDING_DELETES = None
    # N382：执行侧删除条目同样属于"本次运行"状态，必须进复位清单，
    # 否则长驻调用（及测试）里上一次的删除会被下一次当成自己删的。
    _RUN_DELETED_PATHS = None
    _DISPATCH_REACHED = False
    # N534（第一百七十五轮，中危）：`_warn_legacy_conflict_dir._done`
    # 是 M79 新加的「只提示一次」，同族的 _conflict_path._warned /
    # _local_exists._warned 都在复位清单里，唯独它不在。而它在单轮内
    # 零作用（clean_conflict_artifacts 只有一个调用点，一轮只清一次，
    # 根本没有「每次清理都刷屏」可防），唯一效果就是**跨轮静默** ——
    # 声明的是防刷屏，实际是静音器（与 N455 / N483 同族）。
    _warn_legacy_conflict_dir._done = False
    _AUTH_PROBING = False
    # N204：no_git 挂在 _RT 单例上，却不在复位清单里 —— 实测置位后
    # 复位仍为 True，于是同进程后续运行**全部禁用 git 通道**。
    # 真实 CLI 是新进程（看不出来），只有长驻调用会命中，
    # 症状是"git 通道莫名其妙不生效"，排查方向完全跑偏。
    # N262：这里只清了 no_git 一个字段，而 _RT 还装着
    # tree_cache / changes_cache / exec_reliable / transport_probe
    # —— 它们都是**按仓库**缓存的，跨用例（甚至跨仓库）残留。
    #
    # 后果不只是"测试串味"：实测 12/30 套件红，而 mutate.py 的判据是
    # 「基线全绿才继续」，于是**整条变异流水线一条都跑不起来**
    # （N274）。清掉这四项后 test_moves / test_committed_newfile 立刻
    # 转绿 —— 它是挡住流水线的那道塞子。
    #
    # 不重建 _RT 实例本身（它可能被别处持有引用），逐字段复位。
    try:
        _RT.no_git = False
        _RT.tree_cache = {}
        _RT.changes_cache = None
        # exec_reliable / transport_probe **刻意不复位**：
        # 它们是「机器/网络事实」而非 per-run 状态 ——
        #   · exec_reliable  执行位在挂载上是否可信（文件系统属性）
        #   · transport_probe 能否 git push（网络 + 认证属性）
        # 每次运行重探是纯浪费：实测本沙盒一次 ls-remote 探测要 ~20s
        # （DNS 解析阶段不吃 curl 的 --max-time），30 套 × 多次运行
        # 直接把套件拖到超时。
        #
        # 跨仓库的正确性由 switch_repo() 负责（N152 已接在配置解析出口），
        # 那里会连这两个一起作废。
    except (NameError, AttributeError):
        pass
    # N168：还有三个 per-run 状态不在清单里。
    #
    # `_BLOCKED_BY_GUARD` 后果最直接：顶层 `if _BLOCKED_BY_GUARD: exit(2)`。
    # 不复位 → 同进程里先跑一次"被拦下"的用例，之后**成功**的用例
    # 也返回 2。而 2 的语义是"被防护拦下、改动没丢"，CI 据此告警 ——
    # 假告警。
    #
    # 更要紧的是它让契约**无法被验证**：退出码 0/2 的区分是 N74/N111
    # 两次修复的目标，只要这个标志会泄漏，任何"退出码应为 0"的断言
    # 都可能假绿或假红，排查方向完全跑偏。
    _BLOCKED_BY_GUARD = None
    _AUDIT_GUARD_DONE = False
    _SELF_ART_GUARD_DONE = False     # M99：不复位的话第 2 次运行不再提示
    # N163：_paginate.last_truncated 只增不减（只在截断时置 True，
    # 从不置 False），也不在本清单里。同一次运行里前一次分页截断
    # 会残留到后一次 —— 与 N143 修的 _detect_deletions.skipped 同类，
    # 那次补了一个函数属性，漏了另一个。
    try:
        _paginate.last_truncated = False
    except AttributeError:
        pass
    del _BRANCH_TRUNC[:]        # N302：分支列表的截断标志（按运行复位）
    # 纯数据流标志（--mine）不复位的话，同进程里下一次普通运行仍会
    # 以为自己在管道模式，分派前的环境提示全部静音 —— 用户拿到一段
    # 没有任何上下文的输出，却不知道为什么。与 N168 复位 Ui.mode 同源。
    global _STREAM_MODE
    _STREAM_MODE = False
    try:
        _UI.suppressed = False
        _UI.events = []
        # N168：mode 也要复位。set_ui(json) 之后不复位，
        # 同进程里后续运行会一直以为自己在 --json 模式。
        _UI.mode = "text"
        _UI.quiet = False
        _UI.json_warned = False
    except BaseException:
        pass
    # N143：漏了 `_detect_deletions.skipped` —— 挂在**函数属性**上的
    # per-run 状态（不在 globals 里，逐个列全局时最容易漏）。
    # 同进程连跑两次时，前一次的"被消毒排除的删除路径"会残留到后一次。
    try:
        del _detect_deletions.skipped
    except AttributeError:
        pass
    _bind_removals()


_HARDENED = set()         # 已尝试收紧权限的路径（避免每次运行重复提示）

_DRY_RUN = False          # --dry-run 置位：save_state 的落盘闸门（N85）

_NO_DELETE = False        # --no-delete 置位（强制关）
_ENABLE_DELETE = False    # --delete 置位（单次开）


def _gitignored_safe(paths, no_index=False):
    """gitignored_set 的安全包装：拿不到就返回 (空集, False)，并**可见**。

    M187（A163，中危）：本次推送路径上原有三处调用 gitignored_set，只有
    一处（删除候选那处）包了 try。另两处是裸调 —— `git check-ignore` 失败
    （git 不可用 / 超时 / 参数超限）时异常**直接穿透到顶层**，真实 CLI 下
    就是一段裸 traceback + 退出码 1。

    决定性实验（把 gitignored_set 换成抛 RuntimeError 的函数）：异常发生在
    「本次推送会真的删除上述路径」这句**之后** —— 用户拿到的是「一半的
    正常输出 + 一个 Python 异常」，既不知道删没删、也不知道为什么停。

    而那处被包住的 `except Exception: pass` 是 **fail-open** —— 不崩，但
    过滤静默失效，被 .gitignore 屏蔽的删除**会真的执行**（保护反向失效）。
    于是同一个函数在两处表现出两种截然不同的失效方向，且都没有面向用户的
    提示。

    统一成本函数：一律不崩（保住既定的可读提示与明确退出码），且失效时
    **看得见** —— 不留下「以为过滤了、其实没过滤」的静默。
    """
    try:
        return set(gitignored_set(list(paths), no_index=no_index)), True
    except BaseException as _e:
        if isinstance(_e, (KeyboardInterrupt, SystemExit)):
            raise
        print("  ! git check-ignore 失败"
              f"（{type(_e).__name__}） —— 本次无法判断 .gitignore 屏蔽，"
              f"按「未屏蔽」处理（被屏蔽的路径可能被一并推送/删除）。")
        return set(), False


def gitignored_set(paths, no_index=False):
    """返回 paths 中被 .gitignore 屏蔽的子集（git 不跟踪的那些）。

    ``no_index``（N610，第二百〇二轮，中危）：加 `--no-index`。

    `git check-ignore` **默认要查索引**：已在索引里的路径，即便匹配
    ignore 规则也**不**被报告（正是下面 docstring 里"只报告未被跟踪的
    路径"那句）。只有 `--no-index` 才会报告。

    删除侧因此出现了一条与意图无关的分叉（真实 git 四组对照，同一个
    文件、同一条 `*.log` 规则，唯一变量是**用户怎么删**）：

      A `rm secret.log`（未 git rm，索引仍在）  → 不报告 → 判"未屏蔽" → **远端照删**
      B `git rm secret.log`（索引已更新）        → 报告   → 判"被屏蔽" → **不删**
      C `git rm --cached`（索引已更新，文件还在）→ 报告   → 判"被屏蔽" → **不删**
      D `git rm` + commit                        → 报告   → 判"被屏蔽" → **不删**

    **A 与 B 是反的**：`rm`（弱信号、工作区随手删）照删，`git rm`（强信号、
    明确且不可逆地取消跟踪）反而不删 —— 决定结果的不是意图，是"有没有
    顺手 git rm 一下"。

    只给删除侧传 `no_index=True`：新增侧不能加（加了会把"被 .gitignore
    屏蔽但已 `git add -f` 强推"的文件判成屏蔽，而脚本对新增侧的既定
    语义是「点名即意图，等同 git add -f，照推」）。

    **一次** `git check-ignore` 查出全部，不要每文件跑一个子进程：
    批量推送上百个文件时那是上百次 git 调用，纯属浪费，
    而且会把测试里「rmtree 后残留 .git」的竞态放大成经常性失败。

    用 `--stdin -z`：输出是 NUL 分隔的命中路径，能区分「哪个被忽略」，
    而不是像 `-q` 那样只给一个「有没有」的退出码。
    退出码 0 = 有命中，1 = 一个都没有，128 = 出错（不是仓库等）。

    注意语义：只报告**未被跟踪**的路径。已跟踪文件即便匹配 ignore 规则
    也照样被 git 跟踪，这里不会报 —— 正是我们想要的（那个文件受自动检测）。
    """
    paths = [p for p in paths if p]
    if not paths:
        return set()
    # 这里的编解码要**双向**处理，不能只管输出：
    #   · 输出（stdout）：路径可能含非 UTF-8 字节 → errors="surrogateescape"
    #   · 输入（stdin） ：把 surrogate 写回子进程时同样要 surrogateescape，
    #     否则 text=True 下默认用严格 UTF-8 编码 stdin，直接抛
    #     UnicodeEncodeError。只修输出不修输入，崩溃只是换个地方（实测）。
    try:
        proc = subprocess.run(
            ["git", "-C", ROOT, "check-ignore"]
            + (["--no-index"] if no_index else [])
            + ["-z", "--stdin"],
            input=("\0".join(paths) + "\0").encode("utf-8", "surrogateescape"),
            capture_output=True, timeout=DEFAULT_GIT_TIMEOUT)
        out = proc.stdout.decode("utf-8", "surrogateescape")
    except (OSError, subprocess.SubprocessError, UnicodeError) as e:
        # N76：空集的语义是「这些文件都没被屏蔽」，即把探测失败表达成
        # 「确认没问题」—— 方向错了。而脚本在同类场景一贯取反方向
        # （local_state_map / detect_changes 用 fatal=True，
        #  _check_attr_batch 失败时先警告再返回空）。
        # 这里是唯一一个既不告警也不 fail-closed 的。
        #
        # 不中断推送（屏蔽信息只是提示），但**必须说出口**：
        # 静默 fail-open 的后果是被屏蔽的文件照常进入推送流程。
        print(f"  ! .gitignore 探测失败（{type(e).__name__}），"
              f"按「未被屏蔽」处理：若有文件实际被忽略，"
              f"本次推送的提示可能不准确。")
        return set()
    if proc.returncode not in (0, 1):
        print(f"  ! git check-ignore 退出码 {proc.returncode}，"
              f"按「未被屏蔽」处理（同上）。")
        return set()
    # N144：原注释写「timeout=30 是全部 11 处 git 调用里唯一的硬编码
    # 短值」—— 那个 30 已在 N76 被改成 DEFAULT_GIT_TIMEOUT，注释没跟上。
    #
    # 这属于脚本自己反复批判的形态（api() docstring 原话："写错的文档
    # 比没文档更糟，维护者会照着它去「修正」实现，把安全的部分改得
    # 不安全"）。照这条注释去找那个 30，会找不到，然后不知道该不该动。
    #
    # 现状：与其它 git 调用同用 DEFAULT_GIT_TIMEOUT。
    # fail-open 的风险仍在（check-ignore 超时会把"探不到"当"未被忽略"），
    # 但那是超时语义本身，不是这个值的长短问题。
    return {p for p in out.split("\0") if p}


def detect_changes(refresh=False):
    """用 git 检测工作区相对本地基线的改动（含未跟踪文件）

    结果会缓存（一次运行内多处使用）。但**写入文件后再检测必须 refresh**：
    pull 合并过程中会改写文件，沿用旧快照会把刚落盘的改动误判成「本地无改动」，
    进而在下一轮合并里被远端版本直接覆盖掉。

    用 -z 避免 git 对含空格/中文/引号的路径做 quoting 转义（省掉反转义）。
    加 --no-renames：重命名会退化成 "D 旧路径" + "?? 新路径"，旧路径被跳过、
    新路径当新增推送，正好符合本脚本「不支持删除」的语义，也绕开了 -z 下
    重命名记录的字段顺序问题。
    """
    if _RT.changes_cache is not None and not refresh:
        return _RT.changes_cache

    # fatal=True：git 挂了就返回空 → 被下游误读成「没有改动」。
    # 那条伪装成成功的空结果，正是 P1-5 要根治的。
    out = git("status", "--porcelain", "-z", "--untracked-files=all",
              "--no-renames", fatal=True)
    fields = out.split("\0")
    paths, i = [], 0
    while i < len(fields):
        item = fields[i]
        i += 1
        if not item or len(item) < 3:
            continue
        xy, path = item[:2], item[3:]     # 标准格式 "XY<space>path"
        if not path:
            continue
        if "D" in xy:
            # 不在这里处理删除：删除条目在建 tree 时才统一算（见那里）。
            # 这里只把它挡在「待推送清单」之外 —— 已删文件没有本地内容可读，
            # 放进 todo 会在建 blob 时读不到文件而失败。
            continue
        if "R" in xy or "C" in xy:        # 防御：理论上 --no-renames 后不会走到
            if i < len(fields):
                path = fields[i]
                i += 1
        paths.append(path)

    _RT.changes_cache = paths
    return paths


# ---------------------------------------------------------------- 本地状态

# _RT.exec_reliable = None  # → Runtime 实例属性（见 Runtime 段）


def _file_is_executable(full):
    """文件自身是否带执行位。

    用 st_mode & 0o111，**不用 os.access(X_OK)**：后者判断的是「当前进程能否
    执行它」，受挂载选项影响 —— noexec 挂载（本沙盒的 virtiofs 就是）下，
    即使权限位是 0755，os.access 也返回 False，于是所有文件都被判成 100644，
   chmod +x 永远推不上去。git 记录的是权限位本身，所以按位判断才对得上。
    """
    try:
        return bool(os.stat(full).st_mode & 0o111)
    except OSError:
        return False


def _exec_bit_reliable():
    """该环境能否靠文件权限位判断可执行（惰性探测，只探一次）。

    两个条件都要满足，缺一不可：
      1. git 跟踪执行位（core.fileMode != false）
      2. 文件系统真的区分权限（不是所有文件都带执行位）

    容器 / 挂载目录常两条都不满足：core.fileMode=false，或权限一律 0777，
    此时推断会把 .md / .json 全判成 100755。
    """
    if _RT.exec_reliable is not None:
        return _RT.exec_reliable

    if git("config", "--bool", "core.fileMode").strip() == "false":
        _RT.exec_reliable = False
        # JSON 模式下必须静音。
        #
        # 本函数是 `local_state_map()` 的必经路径，而 --status / --scope
        # 都在**打印 JSON 之前**调它 —— 这行 print 会落在 JSON 前面，
        # 让整体不可解析（实测：容器里 core.fileMode=false 时，
        # `--status --json` 首行就是这句话，json.loads 直接失败）。
        #
        # 触发条件恰恰是最常见的部署形态（README：「容器/挂载目录常两条
        # 都不满足」），于是那条被 README 承诺为「结构化输出目前只在
        # --status 上可用」的渠道，在这类环境里**恒不可用** —— 而测试机
        # fileMode=true 从不触发，所以全套回归照样全绿。
        #
        # 与 N99 / M80 是同一条判据的第三处：「JSON 模式下分派前的任何
        # 输出都必须静音」。前两处各修了眼前那一处，这里没跟上
        #（"修一处 ≠ 修全部"，与 N348 / M50 同族）。
        #
        # 纯数据消费者不该被迫去剥人类文案（show_status docstring 原话）；
        # 这是环境提示而非数据，静音不丢判定依据（返回值照旧）。
        # 用 _stream_active() 而非 _json_active()：--mine 的管道同样
        # 会被这行污染（实测：那行被 while read 当成文件名）。
        if not _stream_active():
            print("  ! core.fileMode=false：git 不跟踪可执行位，"
                  "新文件一律按 100644 处理")
        return _RT.exec_reliable

    # 挑明显不该可执行的已跟踪文件探一下文件系统。
    #
    # 取**多个样本**而不是第一个：单个样本可能是被合法 chmod +x 的
    # （少见但完全可能），那样整个环境会被误判成「文件系统不区分权限」，
    # 于是所有新文件按 100644 推送 —— 可执行位静默丢失，而提示只打印一行
    # 与具体文件无关的话，没人会联想到是探测踩错了样本。
    # 判定「所有样本都带执行位」才算环境不可靠，并打印文件名便于核对。
    samples = []
    for line in git("ls-files", "-z").split("\0"):
        if not line.endswith((".md", ".txt", ".json", ".py", ".yml", ".yaml")):
            continue
        probe = os.path.join(ROOT, line)
        if os.path.isfile(probe):
            samples.append(probe)
        if len(samples) >= 5:
            break

    if not samples:
        # 零样本 = 完全没探测过，却要返回 True（宣称可靠）—— 后果是
        # 整个仓库的权限位被静默翻转（实测：无 .md/.txt/.json/.py/.yml
        # 的仓库里所有文件 0777 → 新文件全被推成 100755）。
        # 宁可丢执行位，也不能在没探测的情况下给出「可靠」结论。
        #
        # 这个分支**必须写缓存**：函数开头靠 `_RT.exec_reliable is not None`
        # 跳过重复探测，而调用点在 local_state_map() 的文件循环里 ——
        # 每个文件一次。不写缓存就是每个文件重新跑一遍
        # `git config` + `git ls-files`（1000 个文件 = 1000 次 git 调用，
        # 沙盒单次 1~5 秒 → 推送拖成小时级）。纯 Go/Rust/C 仓库里
        # 一个 .md 都没有，零样本是常态而不是边缘情况。
        _RT.exec_reliable = False
        return _RT.exec_reliable
    flagged = [p for p in samples if _file_is_executable(p)]
    if len(flagged) == len(samples):
        # N88：「所有样本都带执行位」不等于「文件系统不区分权限」。
        # .py 样本全被合法 chmod +x 是完全正常的（脚本就该可执行），
        # 而原判据把它和"环境一律 0777"混为一谈 ——
        # 实测 5 个 .py 均为可执行脚本时，被误判成环境不可靠，
        # 于是**新建脚本的 +x 被静默丢掉**（推成 100644）。
        #
        # 补一道验证：造一个临时文件，chmod 成 0600，看它是否真的不带
        # 执行位。这直接测"文件系统是否区分权限"，不受样本内容影响。
        # 只有**两者都成立**才判不可靠：样本全带 x **且** 0600 也带 x。
        _probe = None
        try:
            import tempfile
            _fd, _probe = tempfile.mkstemp(dir=ROOT, prefix=".exec_probe_")
            os.close(_fd)
            os.chmod(_probe, 0o600)
            _fs_distinguishes = not _file_is_executable(_probe)
        except OSError:
            _fs_distinguishes = True        # 探不了就不据此下结论
        finally:
            if _probe:
                try:
                    os.unlink(_probe)
                except OSError:
                    pass
        if _fs_distinguishes:
            # 文件系统正常 —— 样本只是恰好都该可执行。判可靠。
            _RT.exec_reliable = True
            return _RT.exec_reliable
        _RT.exec_reliable = False
        names = ", ".join(os.path.basename(p) for p in flagged[:3])
        print(f"  ! 文件系统不区分权限（探测了 {len(samples)} 个文件，"
              f"{names} 等全部带执行位，且 0600 的临时文件也被判为可执行）："
              f"新文件一律按 100644 处理")
        return _RT.exec_reliable

    _RT.exec_reliable = True
    return _RT.exec_reliable


def local_state_map():
    """本地 path → (mode, sha)：以 git 索引为唯一权威来源。

    新文件（索引里没有）不靠 os.access 推断——先过 _exec_bit_reliable()，
    环境不可靠时**回退 100644**：宁可丢执行位，也不要把文档推成可执行。

    必须 **refresh=True**：本函数的语义是「当前工作区的状态」，而
    detect_changes() 的结果带缓存。若缓存在文件被写入**之前**已填充，
    这里就会拿到一份过期的列表 —— 于是「刚改过、却不在列表里」的文件
    落到下方 `m[rel] = (mode, parts[1])` 分支，沿用 **git 索引里的旧 sha**。

    后果正是本脚本最想防的那类事故：本地改动被判成「已与远端一致」而跳过，
    **静默丢失，零报错**（实测复现）。宁可多跑一次 git status 也要拿新值。
    顺带把全局缓存刷新成最新，后续调用一并受益。
    """
    m = {}
    # fatal=True：与 detect_changes 同理。ls-files 失败返回空 → lmap={}
    # → 所有文件都「本地不存在」→ 后续判定全线失真。
    # 用 -z（NUL 分隔）：路径里可能出现 tab 或换行，按行分割会错位。
    # 与 detect_changes 的 `git status --porcelain -z` 保持同一解析口径 ——
    # 两处口径不一致时，含特殊字符的路径在一处正常、另一处错位，
    # 排查起来极难关联。
    for rec in git("ls-files", "-s", "-z", fatal=True).split("\0"):
        if not rec:
            continue
        if "\t" not in rec:
            continue
        meta, path = rec.split("\t", 1)
        parts = meta.split()
        mode = parts[0] if parts[0] in VALID_MODES else "100644"
        m[path] = (mode, parts[1] if len(parts) > 1 else "")

    changed = detect_changes(refresh=True)
    # symlink 走单独路径（要读链接目标而非目标文件内容），
    # 普通文件用批量 hash-object 一次算完。
    plain = []
    for rel in changed:
        full = os.path.join(ROOT, rel)
        if os.path.islink(full):
            m[rel] = ("120000", local_blob_sha(rel))
        elif os.path.isfile(full):
            plain.append(rel)
    shas = local_blob_shas(plain)
    for rel in plain:
        full = os.path.join(ROOT, rel)
        if _exec_bit_reliable():
            # 执行位可靠时以**工作区**实际权限为准。
            # 只 chmod +x 的场景下 git status 会报 M，但索引里的 mode 还停在
            # 旧值；沿用索引的话「改成可执行」会被推成一个没变化的 100644。
            mode = "100755" if _file_is_executable(full) else "100644"
        else:
            # 环境不可靠（容器/挂载常 0777）：索引是唯一可信来源，
            # 宁可丢执行位，也不要把 .md / .json 全推成可执行。
            idx = m.get(rel)
            mode = idx[0] if idx and idx[0] in ("100644", "100755") else "100644"
        m[rel] = (mode, shas.get(rel) or local_blob_sha(rel))
    return m


def _local_matches_baseline(state, lmap, rel):
    """本地文件与基线记录完全一致 → 本地没改过。

    用于区分两种「本地 ≠ 分支」：
      · 本地改过（相对基线变了）   → 是要推的改动
      · 本地没改、只是落后于主干   → 推它只会把主干回退成本地旧版本

    基线里没记录的返回 False（保守）：那种情况归第一层的
    「基线未记录」分支处理，不算「已知未改动」。
    """
    base = (state.get("files") or {}).get(rel)
    if not isinstance(base, dict) or not base.get("sha"):
        return False
    return lmap.get(rel) == (base.get("mode"), base.get("sha"))


def pending_deletions(state, lmap, rstate=None):
    """待删除清单：基线有、本地无、本地曾有过、远端还在的文件。

    **不依赖插件**：插件默认关闭（防任意代码执行），但「哪些文件该删」
    这个信息不该跟着一起消失 —— 否则本地删了文件、推送成功、远端还留着，
    而输出一个字都不提。使用者（尤其是 AI 推送）会以为删干净了。

    本函数只**报告**，绝不删除。删除不可逆，必须由人/AI 核查确认后手动执行。

    判据四信号交集（缺一不可）：
      基线有        它曾经被同步过
      本地无        本地已经删掉了
      local_seen 有 本地确实曾经有过（不是「远端独有、本地从没有过」）
      远端有        远端确实还留着（删了才有意义）

    rstate 为 None 时跳过「远端有」这一条，只在还能确认的维度上判断。
    """
    baseline = state.get("files") or {}
    seen = set(state.get("local_seen") or [])
    out = []
    for p in baseline:
        if p in lmap:
            continue                      # 本地还在，不是删除
        if p not in seen:
            continue                      # 本地从没有过，远端独有
        if rstate is not None and p not in rstate:
            continue                      # 远端已经没了
        out.append(p)
    return sorted(out)


def pending_deletes_union(state, lmap, rstate=None):
    """待删除清单：**A、B 两源的并集**（N306，第九十一轮，中危）。

    两个来源恰好互补、各自看不见另一半：

      A ``pending_deletions``  看不见**未 commit 的删除**
                               （lmap 以 git 索引为唯一权威，
                                `rm a.txt` 后索引里仍在）
      B ``_detect_deletions``  看不见**已 commit 的删除**
                               （它读 git status，commit 后工作区干净）

    少一路就漏一种，所以凡是要「告知用户哪些文件待删」的地方都必须取并集。

    为什么单独抽出来：N284 只在 `--status` 那处做了并集，而
    **出口登记**与**出口重算**两处仍只用 A（第九十一轮实测）。
    于是：

        执行链   _pending_dels ← _detect_deletions（B 源）
        告知链   _pd           ← pending_deletions（A 源）

    **执行用 B 源，告知用 A 源。** 用户 `rm a.txt`（未 git rm）后推送
    ——`_pending_dels` 含 a.txt（会删），而出口清单 `_pd` 为空（不提示）。
    而出口上方那段注释，恰好把这个场景定为**这段代码的存在理由**：

    > 本地删了文件却无其他改动 → 完全不提示 → 远端永久残留，
    > 而使用者（尤其 AI）以为删干净了。

    #83 在 --status 里承认了「A 对未 commit 的删除恒盲」，却没把同一
    结论用到这两处。于是那条注释描述的失效场景**依然完整存在**。

    **告知与执行必须同源**，否则又是一次「声明用一套、执行用另一套」。
    B 侧补「基线里有」与「远端仍在」两条（N285：文本那一路原本报
    「远端仍在」却从不查远端，会算出幽灵条目）。

    ``rstate`` 为空（远端树没取到）时 B 源**不做**「远端仍在」过滤 ——
    这是有意保留的取舍：取不到远端还从严过滤，等于把"不知道"当成
    "没有"，静默少报；而少报正是本函数被抽出来要消灭的那件事。
    多报的那部分在真正执行删除前还会再过一遍判据，方向是漏删不是误删。

    N354（第一百一十轮，低危）：这里原先有第 4 个形参 ``head_sha``
    （"rstate 为空时自己拉一次远端树"），但**四个调用点无一传入**，
    docstring 也通篇未提 —— 逃生舱从未开过，是死形参。而它唯一想救的
    场景（远端树取不到）里 ``remote_state_map`` 同样取不到，传了也还是
    空。删掉，取舍留在上面这段。
    """
    out = list(pending_deletions(state or {}, lmap or {}, rstate) or [])
    try:
        b = [p for p in _detect_deletions()
             if p in ((state or {}).get("files") or {})]
    except BaseException:
        b = []
    if b:
        rs = rstate
        # N285：B 源也要过「远端仍在」，否则会算出幽灵条目。
        # rstate 为空（远端树没取到）时**不过滤** —— 见 docstring 那段取舍。
        if rs:
            b = [p for p in b if p in rs]
    # 并集只增不减：判据更严的那份（A）绝不能被更松的那份顶掉（N244）。
    return sorted(set(out) | set(b))


def report_pending_deletes(paths, where="推送", emit=None, judged=True):
    """把待删除清单说清楚，并要求人工核查确认。

    emit：
      None（默认）→ 立即打印
      False       → 只登记到运行出口，由出口统一打印（不在此处输出）

    judged（N171）：这份清单的判据**是否完整**。
      True  → 来自 pending_deletions：基线有 & 本地无 & local_seen 有
              & 远端还在，四者齐备 —— 可以断言"会删除"。
      False → 来自 baseline_ghosts：只查了"基线有 & 本地无"，
              **没有 local_seen 这一条**，里面可能混着"本地从未有过、
              纯属远端独有"的路径。
              这类路径根本不会被删除，对它们说"本次推送会真的删除"
              是假声明 —— 与 N162 是同一类错误。

    where（N162）：调用语境。
      "--status" 等只读命令下，说"本次推送会真的删除"是**假声明** ——
      只读命令压根不会推送。此前 where 形参零引用，于是只读场景
      照抄推送文案。

    """
    # N213：这段原本是**第二个字符串字面量**（不是 docstring ——
    # 前面已有可执行语句，Python 只会把第一个字面量当 __doc__）。
    # 于是：
    #   · help() / __doc__ 看不到它（误导：以为设计要点没记下来）
    #   · 每次调用都构造一遍再丢弃（无谓开销）
    #
    # 三条设计要点：
    #
    # 1. **高度可见**：独立分隔块 + emoji。混在推送流程的几十行输出里，
    #    人和 AI 都会漏看 —— 而漏看的后果是远端永久残留。
    # 2. **一次运行只出现一次，且在最后**：打印时机由运行出口统一负责，
    #    避免同一份清单在"推送前"和"收尾"各打一遍。
    # 3. **不代劳**：删除不可逆，且远端残留里可能混有别人的改动
    #    （比如别人刚建的文件恰好同名）。自动删会出事。
    if emit is False:
        # N244：原先是**覆盖**赋值。而本函数有多个登记调用点
        #（_main 里的 _pd、report_moves 里的 rest），口径不同：
        #   _pd  查了 local_seen（判据完整、更严）
        #   rest 未查（更松）
        # 后调的覆盖先调的 → 少报与多报同时发生。
        #
        # 改成**并集**：登记口径只增不减，出口统一打印一次。
        # 判据更严的那份绝不能被更松的那份顶掉。
        global _RUN_PENDING_DELETES
        _prev = _RUN_PENDING_DELETES or []
        _RUN_PENDING_DELETES = sorted(set(_prev) | set(paths or []))
        return

    # N227：这里原本是**第二个字符串字面量**（不是 docstring ——
    # 前面已有可执行语句，Python 只把第一个字面量当 __doc__）。
    # 于是它：
    #   · 每次调用都构造一遍再丢弃
    #   · 内容还是**旧版**文案（"明确行动指令"），与真 docstring 里的
    #     "一次运行只出现一次，且在最后"**互相矛盾** ——
    #     读代码的人会看到两套设计意图，不知道该信哪个。
    #
    # N213 只转掉了另一段，这段漏了。现一并改成注释。
    if not paths:
        return
    # N624（第二百〇六轮，中危）：本函数原先 26 处全是裸 `print`。
    # 而同文件 6221 行（N421 / M86）处理**同一件事的另一个出口**时已经
    # 改走 Ui，注释原话是「静音会让 json 消费者完全看不到这一句」。
    # 于是同一份脚本里两条出口一条合规一条不合规，且 #446 恰恰是在
    # **不合规的那一支**里新增了 3 行 print。
    #
    # 后果是双向的：机器读不到（`--json` 是脚本自述"AI / CI 读结论的
    # 唯一可靠途径"），且文本落在 JSON **之前**使整份输出不可解析
    # （实测 json.loads 报 Expecting value: line 2 column 1）。
    # 改走 Ui：json 下进 events（信息不丢），text 下输出逐字不变。
    _ui = get_ui()
    _ui.line()
    _ui.line("=" * 62)
    # N139：删除功能**开启**时，这份清单照样无条件打印，
    # 而正文却写「本脚本**不会**自动删除」—— 与事实相反：
    # 开启后推送就会真的删（--force-delete 还会绕过判据）。
    # 用户读到这句会以为安全、不去核查，于是误删。
    # 标题与正文必须随开关变化。
    _on = not _deletes_disabled()
    # 只有在「判据完整」且「确实是推送语境」时，才能说"会真的删除"。
    # 任一不成立，就只能说"需核查"。
    # N246：--dry-run 也属只读语境 —— 它没有真正推送，
    # 不该出现"本次推送会真的删除"这类措辞。
    _readonly = (str(where).startswith("--") or "status" in str(where)
                 or _dry_this_run())
    _will_delete = _on and judged and not _readonly
    _ui.line(f"  ⚠️  待删除清单（{len(paths)} 个文件）—— "
          + ("推送时**会自动删除**" if _will_delete
             # 标题不能写死"手动删除"：下面三支文案分别是
             # 「可让本脚本删除」/「可能混有不会删的」/「真正推送时会被删」，
             # 哪一支都不是"只能手动"。写死就与正文打架（N346 同形：
             # 同一段输出里两句互相矛盾）。中性措辞三支通用。
             else "需要人工核查确认"))
    _ui.line("=" * 62)
    _ui.line("  这些文件本地已不存在，但远端仍然保留：")
    for p in paths[:30]:
        _ui.line(f"     - {p}")
    if len(paths) > 30:
        _ui.line(f"     ... 还有 {len(paths) - 30} 个")
    # M194（A166，中危）：清单里会装进被 .gitignore 屏蔽的路径 ——
    # pending_deletions 的判据是「基线有 & 本地无 & local_seen 有 & 远端有」
    # 四条，全文没有 .gitignore 任何字样。而 plan_removals 里的
    # `ignore(p)` 对 gitignored 路径**无条件从删除清单里剔除**（force 形参
    # 不参与判断），于是 --delete 对它们**同样无效**。
    #
    # 最典型触发：先推了个日志 → 把 *.log 加进 .gitignore → 删掉本地那份。
    # 于是每次出口都打「可让本脚本删除」，照做后远端还在、清单照旧 ——
    # 这段注释上方自己描述的「功能退化成扫描器」正是这个状态。
    # 已照做的人更难自救，因为他已经"照做了"。
    _ig_exit = _gitignored_safe(paths)[0]
    if _ig_exit:
        _ui.line(f"  其中 {len(_ig_exit)} 个被 .gitignore 屏蔽 "
              f"（{', '.join(sorted(_ig_exit)[:5])}）："
              "**即使开启删除也删不掉**，"
              "需要先取消屏蔽或改用网页端。")
    _ui.line()
    if _will_delete:
        _ui.line("  删除功能当前**已开启**：本次推送会真的删除上述路径。")
        _ui.line("  删除不可逆，且可能与他人改动冲突 —— 请确认后再推。")
        _ui.line("  只想看清单、不删除：加 --no-delete（强制关闭）。")
    elif _on and not judged:
        _ui.line("  删除功能已开启，但本清单**未按 local_seen 收敛** ——")
        _ui.line("  里面可能混有本地从未拥有过的路径（不会删）。")
        # N561（第一百八十三轮，低危）：上面这句「请看本次运行结束后的
        # 待删除清单」在**出口**（where="本次运行"）打印时会**自指** ——
        # 用户正在看的就是那份清单，被要求去看它自己。
        #
        # 成因：`report_moves` 用 `judged=False` 调本函数是对的（rest 来自
        # baseline_ghosts、未查 local_seen，不能断言"会真的删除"），但
        # 接上的这段文案是写给"流程中段的一句提示"的，被出口复用就自指了。
        if str(where) == "本次运行":
            _ui.line("  本次运行的出口清单就在这里；其中本地从未拥有过的"
                  "路径不会删。")
            _ui.line("  要确认会删哪些，请用 --status 看判据完整的那一份。")
        else:
            _ui.line("  要确认「真正会删哪些」，请用 --status 看按判据收敛后的"
                  # M151（A141，低危）：上面这句原本把**本清单**立为
                  #「确认会删哪些」的权威依据，而本清单**未按点名范围
                  # allowed 收敛**（A141 §五 端到端实测：点名 `a.txt ./b.txt`
                  # 时预览 1 个、本清单 2 个）。用户照它去远端手工删，
                  # 会多删已经被点名范围排除掉的那一个 —— 而**预览那一处
                  # 恰恰是对的**。方向是"多删"，对不可逆操作不能这么指。
                  "那一份（本清单未收敛，可能多列）。")
    elif _on and _readonly:
        _ui.line(f"  删除功能已开启；{where} 是只读命令，**不会**删除任何东西。")
        _ui.line("  真正推送时，上述路径会被删除 —— 请确认无误再推。")
    else:
        _ui.line("  本脚本默认**不会**自动删除（删除不可逆，且可能误伤他人改动）。")
        # 出口清单是**唯一保证用户会读到**的一份 —— 本函数的设计要点 2 就
        # 写着「一次运行只出现一次，且在最后」（最后出现的才躲不掉）。
        #
        # 而这里此前是全脚本**唯一不给开启方式**的一处：
        # _delete_how_to_enable() 在 --status 侧、熔断侧等五处都调了，
        # 唯独这份最显眼的清单只说「请手动删除或到网页端操作」。
        #
        # 后果不是"少一行提示"，而是**功能退化成扫描器**：
        # 用户不知道脚本自己就能删 → 没人会去手工删 → 清单每次运行都打、
        # 每次都不解决 → 远端残留永久化，而提示本身也变成噪音被无视。
        #
        # 「默认不删」是对的取舍（docstring 已论证：误删成本不对称），
        # 但取舍成立的前提是**用户知道怎么开**。不知道就等于永远关着。
        # 与 N350 同形：告知了问题，却没给解决办法。
        _ui.line("  确认无误后，可让本脚本删除（本次 / 长期二选一）：")
        if not judged:
            _ui.line("  注意：这份清单**未按 local_seen 收敛** —— 里面可能混有"
                     "本地从未拥有过的路径（即使开启删除也删不掉）。")
        # N625（第二百〇六轮，中危）：这一支只由 `_on` 决定、**不看
        # judged** —— 而出口恒定以 judged=False 调用（14781 / 7551），
        # 删除又默认关闭，于是真实路径就是这一支。
        # 后果是反向的：给了一份更强的行动指引（去开开关让脚本删），
        # 同时把「这份清单没查 local_seen，里面可能混着根本不会删的
        # 路径」整维丢掉。用户照做开启后那些路径照样删不掉。
        _print_how_to_enable(6)
        _ui.line("  也可逐个手动删除，或到 GitHub 网页端操作。")
        # N491（A121，低危）：删除关闭时这份清单会**永久**停留 —— 而它
        # 的一个常见成因是「改名到了被 .gitignore 屏蔽的新路径」（N489）。
        # 那种情形下用户唯一能做的就是取消屏蔽：屏蔽一解除，新路径就会
        # 被推送，旧路径才谈得上删除。输出原先不给这个方向，于是清单
        # 变成幽灵路径长期挂着（与 N421 同族，成因不同）。
        #
        # 这里无法精确点名（删除关闭时 `_plan_removals` 不跑，拿不到
        # shadowed），只给**方向**：让"为什么删不掉"有可查的去处。
        _ui.line("  若其中某个文件你只是**改了名**：检查新文件名是否被 "
              ".gitignore 屏蔽 —— 被屏蔽的新路径不会上传，"
              "旧路径就会一直留在这里。")
    # N110：这句是**虚假承诺**。--status 是只读命令，从不落盘：
    # 它改的是内存里的 state，函数返回后即丢弃。
    # 于是"已记入基线 / 下次仍会提示"都不成立 —— 下次提示是重新算的，
    # 不是因为记过。而用户读到这句会以为脚本替他记住了。
    #
    # 改成如实说明：清单每次实时计算，不依赖记忆。
    _ui.line("  清单每次运行实时计算；远端删掉后本提示自然消失。")
    _ui.line("=" * 62)
    _ui.line()


def baseline_ghosts(state, lmap):
    """基线里有、本地却已不存在的路径（幽灵路径）。

    这是「文件搬家」的直接产物：文件搬走后，旧路径从本地消失，
    但基线与**远端都还留着它**。实测连续搬 4 次，远端会留下 5 份副本，
    且脚本从头到尾不提这件事，推送照样打 ✅。

    注意与「远端删了」区分：本函数只看本地有没有，
    远端是否存在由调用方用 rstate 判断。
    """
    baseline = state.get("files") or {}
    return sorted(p for p in baseline if p not in lmap)


def _warn_move_detection_unavailable(state, ghosts, missing=None):
    """M133（A122）：`local_seen` 判据用不上时，把"搬家检测本次没跑"说出来。

    只告知、**不改判据**：判据整体放弃是脚本自己的取舍（宁可不认、
    不认错；不认的结果是把旧路径拉回来，是"多一份"而不是"少一份"）。
    改判据会把这个取舍翻过来 —— 拿"少一份"的风险换一行输出，不划算。

    `missing`：本次因「不在 local_seen 里」被筛掉的幽灵路径。
      None（默认）＝ 判据整体不可用（`local_seen` 为空）；
      非空列表  ＝ **部分缺失**（N577）—— `local_seen` 里有内容，
                   但恰好缺本次要判的这几条。

    一次性（全局标志）：`detect_moves` 在一次运行里会被 pull
    （现场推断）与推送前的报告多处调用，逐次打印等于刷屏。
    标志必须进 `reset_per_run_globals()` —— 否则同进程第二个用例
    就再也打不出来了（与 `_REWIND_NO_BASELINE_WARNED` 同族）。
    """
    global _MOVE_DETECT_UNAVAILABLE_WARNED
    if _MOVE_DETECT_UNAVAILABLE_WARNED:
        return
    _MOVE_DETECT_UNAVAILABLE_WARNED = True
    if not ghosts:
        return            # 没有幽灵路径 → 没有"本来可能认出"的东西，不打扰
    _ls = (state or {}).get("local_seen")
    if missing:
        # N577（第一百八十九轮，中危）：原函数的触发条件是「`local_seen`
        # 整个为空」，而**判据实际失效**的条件是「幽灵路径不在 local_seen
        # 里」—— 两者差了整整一层。只要 `local_seen` 里有任何一个路径，
        # 后半句的过滤就会把幽灵全部筛掉，而 M133 **一句都不会说**。
        #
        # 而部分缺失恰恰是**最典型**的形态：`migrate_state` 对老状态文件
        # 的补齐口径是「当前本地 ∪ 本地 git 历史里删过的」，它至少会把
        # 「当前本地」写进去 → `_seen` 非空 → M133 恒不触发。也就是说
        # M133 宣称要消灭的「与'确实没搬家'完全无法区分」，在它自己点名
        # 的首要场景（老状态文件升级）里**原样存在**。
        _why = (f"只记了 {len(_ls or [])} 个路径，本次要判的 "
                f"{len(missing)} 个幽灵路径都不在其中")
    elif _ls is None:
        _why = "字段缺失（升级前的状态文件）"
    elif isinstance(_ls, list) and not _ls:
        # N580：原先这一支也被报成「字段被污染成非路径集合（如字符串）」，
        # 而真实成因是一个**合法的空列表** —— 名字叫错了，用户会去找
        # 一个根本不存在的"污染"。
        _why = "是空列表（尚未记录任何本地路径）"
    else:
        _why = "被污染成非路径集合"
    # N579（中危）：这三行走的是裸 `print()`，插在 JSON 主体**之前** ——
    # 实测 `--dry-run --json` 的 `json.loads` 直接失败
    # （Expecting value: line 1 column 3），而 JSON 主体本身是完好的。
    # 同文件 L7399 那处（N421 / M86）已经论证过「改走 Ui 通道：json 模式
    # 下它进 events（**信息不丢**），文本模式输出逐字不变 —— 比静音好」，
    # 距离这里只有约 1500 行。M133 既没走 Ui，也没遵守那份论证。
    _ui_now = get_ui()

    def _say(t):
        if _ui_now is not None:
            _ui_now.line(t, "warn")
        else:
            print(t)

    _say(f"  ! 搬家检测本次不可用：local_seen {_why}，"
         f"无法区分「你搬走的」与「远端独有的」。")
    # N578（中危）：原先只说了「多一份」那一半（--pull 会把旧路径拉回来），
    # 而实测发生的还有**相反的一半**：这些路径进不了
    # `pending_deletes_union` → `rest` 被筛空 → 脚本自述「远端永久残留」
    # **唯一**的告知渠道（`{N} 个路径远端仍保留副本`）整段不打印。
    # 只说一半，等于告诉用户"最坏是多一份"，而实际可能是"永久遗留、
    # 且无人知晓"。两个方向都必须写出来。
    _say(f"    影响一（少认）：{len(ghosts)} 个本地已消失的路径一律当作"
         f"未搬家处理，--pull 会把它们拉回来（宁可多一份、不漏拉）。")
    _say("    影响二（少报，与上面方向相反）：这些路径也不会进入"
         "「N 个路径远端仍保留副本」清单 —— 那是它们唯一的告知渠道，"
         "本次归零，等于永久遗留且无人知晓。")
    # N580（低危）：`--init-baseline` 是死路 —— 实测它回答「基线已存在，
    # 未做任何改动」、`local_seen` 长度不变；脚本自己的输出把用户指向
    # 的是 `--reset-baseline`（实测 `--reset-baseline --yes` 才重建）。
    # 与 N494 / N486 同族：remedy 指向一条必然失败的命令。
    _say("    恢复：下次成功同步会重建 local_seen；"
         "要立刻重建请加 --reset-baseline（实测 --init-baseline 在基线"
         "已存在时是 no-op，不会重建）。")


def detect_moves(state, lmap, will_push=None):
    """检测搬家（重命名）：旧路径消失 + 新路径出现且内容相同。

    返回 (moves, ghosts)
      moves  = [(old_path, new_path)]  判定为搬家的配对
      ghosts = [path]                  本地已消失、但配不上新路径的

    判据用 **blob sha 相等**（内容寻址，与 git 的重命名检测同源）。
    搬家同时改了内容时 sha 不同 → 配不上 → 只出现在 ghosts 里。
    这是保守的：宁可认不出，也不能把「删了 A、新增无关的 B」误判成
    搬家 —— 那会导致 pull 跳过该拉的文件（数据缺失）。

    N372（第一百一十八轮，中危）：**声称要防的场景，恰好是它自己造成的**。
    上面那句"宁可认不出"只落实了 sha 相等这一半，缺 `plan_removals._guard`
    与 `detect_renames` 都有的那两条判据：

      判据 3 —— 本地必须**曾经拥有过**旧路径
      判据   —— 新路径**会被推送**（`will_push`）

    缺判据 3 的后果链（实测，见 测试/test_moves_local_seen.py）：
      · 幽灵集合来自 `baseline_ghosts`，它只看「基线有 & 本地无」；
      · 而基线与远端一致，`init_baseline` 会把**远端独有**文件一并记进
        基线（N238/N277 反复论证过这一点）—— 于是「远端独有、本地从未
        有过」的路径**天然是 ghost**；
      · 只要本地恰好有一个同内容的新文件，就配成「搬家」；
      · 配对结果写进 `state["moved_away"]`，pull 优先读它并**跳过**该
        路径（`rel in _moved_away` → 不拉回旧路径）；
      · 而 pull 的候选集是所有「`p not in lmap`」的远端路径 —— 远端
        独有文件本来必然在里面。
      → **别人在远端新增或修改的文件，本地永远拉不到，且脚本报告
        「已搬到 …，跳过」。这是数据缺失。**

    判据 3 的过滤与 `plan_removals._guard` 严格同源：
      · `local_seen` 非空 → 只认 `p in local_seen`
      · `local_seen` 为空/缺失 → **判据不可用，一条都不认**
        （无法区分「远端独有」与「本地曾有过」，与 plan_removals 的取舍
        一致：宁可不认，不认错。不认的结果是把旧路径拉回本地，
        是"多一份"而不是"少一份"）

    ⚠️ 不要拿 `state["files"]` 给 local_seen 兜底：files 就是基线的远端
    文件清单，用它兜底等于**取消判据 3**（与 N406 同源）。

    will_push：本次会推送的路径集合。`None`（默认）= 不过滤，行为与
    修复前一致（pull 侧的兜底推断走这条，它没有"本次推送"的概念）。
    非 None 时，新路径不在其中就不配对 —— 否则「旧路径随搬家被记录、
    新内容却没推上去」= 内容真的丢了（`detect_renames` 的 H1 就是为此）。
    """
    baseline = state.get("files") or {}
    ghosts = baseline_ghosts(state, lmap)
    if not ghosts:
        return [], []

    # 判据 3：本地必须曾经拥有过（与 plan_removals._guard 同源）。
    # 必须在配对**之前**过滤，而不是配对后再筛：ghosts 的口径本身就是
    # 「基线有 & 本地无」，不过滤的话远端独有文件会先被配成对、再拿
    # 判据筛，中间那一步已经把错误结论交给 report_moves 与 pull 了。
    _seen = _as_path_set(state.get("local_seen"))
    if not _seen:
        # 判据不可用 → 一条都不认（口径见 docstring）
        #
        # M133（A122，低危）：这里**整体静默退化** —— 判据 3 依赖
        # `local_seen`，取不到时（字段缺失 / 被污染成字符串 →
        # `_as_path_set` 都返回空集）真搬家也一条不认。
        #
        # 方向是安全的（漏报 → pull 把旧路径拉回来，是"多一份"不是
        # "少一份"），所以不动判据；但**不告知**有个具体后果：
        # 用户看到的是"脚本没说话"，与"确实没搬家"**完全无法区分**。
        # 而 N372 的收益在这批状态文件上恰恰拿不到 —— 用户会以为
        # 防护在生效，其实它本次压根没跑。
        _warn_move_detection_unavailable(state, ghosts)
        return [], list(ghosts)
    # N577（第一百八十九轮，中危）：`_seen` 非空**不等于判据可用**。
    # 真正的失效点就是下面这行 —— 幽灵路径不在 `local_seen` 里就被整条
    # 筛掉，而 M133 的触发条件写的是「`local_seen` 整个为空」，于是
    # **部分缺失时 100% 静默**。
    #
    # 实测三角对照（真实远端 900 文件、3 个幽灵，唯一变量是
    # `local_seen`）：
    #   A 含全部 4 个  → 不打扰（正确），残留清单 3 个
    #   B 全空         → 打印，残留清单**消失**
    #   C 只含 a.txt   → **一句都不说**，残留清单**消失**
    # C 组不是边缘形态：`migrate_state` 至少会把「当前本地」写进
    # `local_seen`（N277 口径），老状态文件升级后天然落在这一组 ——
    # 即 M133 自己点名的首要场景里，它原样失效。
    #
    # 只补告知、不动判据：把不在 `local_seen` 里的幽灵**认成搬家**才是
    # 危险动作（会跳过该拉的文件 = 数据缺失），宁可继续漏报。
    _missing_seen = [p for p in ghosts if p not in _seen]
    if _missing_seen:
        _warn_move_detection_unavailable(state, ghosts,
                                         missing=_missing_seen)
    ghosts = [p for p in ghosts if p in _seen]
    if not ghosts:
        return [], []

    # 新增路径：本地有、基线没有
    # M14（A14 / A23 实证）：配对结果**随输入顺序变化** ——
    # `added` 与 `ghosts` 都来自 dict 迭代，同一组输入换个顺序
    # 会配出不同的 (old, new) 对。N5 已修过同族一处，这里漏了。
    #
    # 排序后再配对：结果确定，可复现、可测试。
    # 排序键用路径本身而不是"谁先出现"，多条同 sha 候选时按字典序取。
    added = sorted(p for p in lmap if p not in baseline)
    # 判据：新路径本次会被推送（None = 不过滤，见 docstring）
    if will_push is not None:
        added = [p for p in added if p in will_push]
    if not added:
        return [], ghosts

    old_by_sha = {}
    for p in sorted(ghosts):
        v = baseline.get(p)
        if isinstance(v, dict) and v.get("sha"):
            old_by_sha.setdefault(v["sha"], []).append(p)

    moves, matched_old, used_new = [], set(), set()
    for new in added:
        lsha = lmap.get(new, ("100644", ""))[1]
        cands = old_by_sha.get(lsha) or []
        for old in cands:
            if old in matched_old or new in used_new:
                continue
            moves.append((old, new))
            matched_old.add(old)
            used_new.add(new)
            break
    rest = [p for p in ghosts if p not in matched_old]
    return moves, rest


def report_moves(moves, ghosts, rstate=None, state=None, lmap=None,
                 allowed=None):
    """把「搬家会让远端留副本」说清楚。

    删除功能**关闭**时脚本不删除远端文件（删除不可逆，不该由同步工具
    代劳），但至少要让用户**知道**远端还留着旧副本 ——
    否则 913 个文件的仓库搬几次家，远端就永久膨胀，而本地一切正常。

    N371（第一百五十轮复核，中危）：上面这句原先**无条件**说
    「脚本不支持删除远端文件」。而删除**开启**时脚本在同一次运行里
    紧接着声明「将删除 N 个远端路径」并**真的删了** —— 同一次输出
    内部自相矛盾，且事实站在矛盾的一侧（实测远端文件确实消失）。

    更值得单独提的是 `--yes` 这个组合：`--yes` 跳过的是**删除确认**，
    而那句「脚本不删除远端文件」恰好是用户在 `--yes` 下唯一还会读到的
    关于删除的说明 —— 它说的是反的。

    state / lmap：可选。给了就把 `rest` 与出口清单同源化（N457），
    不给则维持原判据（不静默改变行为）。
    """
    if not moves and not ghosts:
        return
    rstate = rstate or {}
    print()
    if moves:
        stay = [(o, n) for o, n in moves if o in rstate]
        if stay:
            # N371 ②（A94 复核）：标题里的「保留副本」与下面两行是
            # **同一句断言**，必须一起分流。
            #
            # 早先只分流了下面两行 —— 于是删除开启时输出变成
            #   「检测到 1 个文件搬了位置（远端旧路径会**保留副本**）」
            #   「删除功能已开启：这些旧路径将在本次推送中删除」
            # 两句话挨着，说的是相反的事。而这正是 N371 要修的那类问题
            # 本身（同一次输出里对同一件事的陈述相反）——
            # 修了一半，等于把自相矛盾从"两处"改成"两行"。
            #
            # A94 原话：只改下面两行会留下「标题说保留、结尾说按删除
            # 判据处理」的自相矛盾。
            # N560（第一百八十三轮，中危）：原来的分流**只看开关、不看
            # `_DRY`**，于是 `--delete --dry-run` 下这句仍写
            # 「这些旧路径将在本次推送中删除（见下方删除确认环节）」，
            # 而：
            #   ① dry-run 根本**不存在**"删除确认环节"（dry-run 的契约
            #      就是不产生副作用，下方出现的是「[dry-run] 删除预览」）
            #      —— 括注指向一个不存在的对象；
            #   ② 同一次输出里 `[dry-run] 未做任何推送` 与出口清单的
            #      「需要人工核查确认」说的是相反的事。
            # 用户无法从这份输出里判断**正式推送到底删不删**。
            #
            # 而同族提示另有 4 处（4665 / 9291 / 9447 / 15081）**都**按开关
            # 分流了，只有这一处既没按开关、也没按 dry-run 分流。
            _dry_now = False
            try:
                _dry_now = bool(_dry_this_run())
            except BaseException:
                _dry_now = False
            # M161（A147，低危）：① 的判据维度原本只有「开关」（N371 补的）
            # 与「dry」（N560 补的）两维，**缺 `allowed`（点名范围）这一维**。
            #
            # 而执行侧（16506 起）点名时会按 allowed 裁剪删除清单，于是
            # `--delete --yes new.txt`（点名只推新路径）下同一份输出里：
            #   ① 本处：「这些旧路径将在本次推送中删除」
            #   ② 执行侧：「N 个删除不在点名范围内，本次不处理（它们仍留在远端）」
            # 两句相反，② 才是事实 —— A147 记录下 tree payload 实证：
            # 送出的删除条目是 **[]**（非点名时是 ['old.txt']）。
            #
            # A147 的 2×2 矩阵（dry × 点名）里 ① 原本错 3 格；
            # 补上 dry（N560）后剩点名两格，本条补完剩 0。
            #
            # 方向是 fail-closed（最坏是"以为会删、实际没删"→ 远端残留），
            # 不会产生误删，故定低危；但它是三句三个方向里**唯一**靠
            # 执行侧裁清单也没能消掉的一句（_trim_pending 只修了出口 ③）。
            _named = allowed is not None
            _in_named = [(o, n) for o, n in stay if not _named or o in allowed]
            _out_named = [(o, n) for o, n in stay if _named and o not in allowed]
            _will_del = (not _deletes_disabled()) and not _dry_now
            print(f"  ! 检测到 {len(stay)} 个文件搬了位置"
                  + ("（远端旧路径会**保留副本**）："
                     if _deletes_disabled()
                     else ("（远端旧路径将在**正式**推送中按删除判据处理）："
                           if _dry_now
                           else ("（远端旧路径**不在本次点名范围内**）："
                                 if not _in_named
                                 else "（远端旧路径将按删除判据处理）："))))
            for old, new in stay[:10]:
                print(f"     {old}  →  {new}")
            if len(stay) > 10:
                print(f"     … 另有 {len(stay) - 10} 个")
            # N371：删除开启时这里说的是反的 —— 下面紧接着就会
            # 声明「将删除 N 个远端路径」并真的删掉。按开关分流。
            if _deletes_disabled():
                print("    脚本不删除远端文件（不可逆操作）。"
                      "旧副本需手工清理，")
                print("    或启用删除支持后按提示处理。")
            elif _dry_now:
                print("    删除功能已开启，但本次是 --dry-run："
                      "**不会**删除任何远端文件。")
                print("    去掉 --dry-run 正式推送时，"
                      "上述旧路径才会按删除判据处理。")
            elif not _in_named:
                print("    删除功能已开启，但这些旧路径**都不在本次点名范围内**"
                      "—— 本次不处理，它们仍留在远端。")
                print("    要一并删除请把这些旧路径也写在命令行上。")
            else:
                print(f"    删除功能已开启：其中 {len(_in_named)} 个旧路径"
                      f"将在本次推送中删除（见下方删除确认环节）。")
            if _out_named and _in_named:
                print(f"    另有 {len(_out_named)} 个旧路径**不在本次点名范围内**"
                      f"，本次不处理（它们仍留在远端）：")
                for _o, _n in _out_named[:10]:
                    print(f"       {_o}  →  {_n}")
                print("      要一并删除请把这些旧路径也写在命令行上。")
    rest = [p for p in ghosts if p in rstate and p not in {o for o, _ in moves}]

    # N457（第一百五十轮，中危）：`rest` 与出口清单是**两套判据** ——
    #   rest   = ghosts ∩ rstate（不查 local_seen、不查 git status 删除）
    #   清单   = pending_deletes_union（A ∪ B，两源都查）
    # 于是计数与清单恒不一致，实测：
    #   rest = ['m_old.txt', 'ro.txt']  → 计数 2
    #   union= ['m_old.txt']            → 清单 1
    # `ro.txt` 从计数里出现、从清单里消失，**没有任何说明**。
    # 用户拿计数去核对清单，只会以为清单被截断了。
    #
    # 而这句承诺的存在理由就是"防止永久遗留、无人知晓"——
    # 计数与清单对不上，等于让这条告知链自己变成噪音。
    #
    # 同源化：rest 只保留判据完整的那批（= 并集的子集）。
    # 副作用正好修掉 N458：远端独有文件（本地从未拥有）不再被说成
    # 「远端仍保留副本」—— 那本来就不是"本地删了、远端没删"。
    #
    # 取不到 union 时维持原判据：宁可多报，也不静默改变行为。
    if state is not None or lmap is not None:
        try:
            _union = set(pending_deletes_union(state or {}, lmap or {},
                                               rstate) or [])
            rest = [p for p in rest if p in _union]
        except BaseException:
            pass
    if rest:
        # 原先只有一行 `! 有 N 个文件…这些路径不会自动清理`，
        # 埋在推送输出里，且**没说该怎么办**。
        # 自动化场景（AI / CI 推代码）会整段漏掉 —— 用户以为推干净了，
        # 实际远端文件一个没删，而且这些路径以后再也不会被本地改动触发
        #（本地没有它们了），等于永久遗留、无人知晓。
        #
        # 时机按开关区分，避免同一份清单打印两遍：
        #   删除**开启** → 现在就打完整清单（即将发生，需要你确认）
        #   删除**关闭** → 只给一行指引（完整清单在推送结果窗口里，
        #                  那才是它该出现的地方）
        if not _deletes_disabled():
            # N244：这里直接调 report_pending_deletes(rest, ...)，
            # 而它内部会 `_RUN_PENDING_DELETES = list(paths or [])`
            # —— **覆盖**掉前面第 8487 行已登记的 _pd。
            #
            # 两份清单口径不同：
            #   _pd  = pending_deletions（查了 local_seen，判据完整）
            #   rest = baseline_ghosts 里远端仍在的子集（未查 local_seen）
            # 用后者覆盖前者 → 少报（_pd 里不在 rest 的条目消失）
            # 与多报（rest 里不在 _pd 的条目混入）**同时发生**。
            #
            # 改成**并集**：登记口径只增不减，出口统一打印。
            # 判据更严的那份（_pd）绝不能被更松的那份顶掉。
            # judged=False：rest 来自 baseline_ghosts（未查 local_seen），
            # 不能断言"会真的删除"（N171）。
            #
            # N174：这里原来是**立即打印**，而下面 8353 行还会登记一次、
            # 出口再打一次 —— 删除开启时恒真重复（两份清单判据不同，
            # 但路径集合必然有交集）。
            #
            # 改为只登记，由出口统一打印一次。
            # 删除开启时"即将发生"的确认诉求，由 8353 前的独立确认环节
            # 负责（那才是该出现的位置）。
            #
            # 并集在 report_pending_deletes 内部做（见该函数），
            # 这里只管调。
            report_pending_deletes(rest, "推送", judged=False, emit=False)
        else:
            # N421（第一百三十五轮，中危）：原先只给一行
            # 「完整清单见本次推送结束后的结果窗口」—— 而那个窗口的
            # 判据是 `if _RUN_PENDING_DELETES:`，删除**关闭**时本分支
            # 不登记，出口恒假，**清单永远不出现**（实测 146 个路径）。
            #
            # 这不只是少打几行：它是「远端永久残留」唯一的告知渠道
            # —— 这些路径本地已不存在，以后再也不会被任何本地改动
            # 触发，等于永久遗留且无人知晓。
            #
            # 删除关闭时这里就是唯一出口，直接打完整清单。
            # 不登记到 _RUN_PENDING_DELETES：那份清单的语义是「待删除」，
            # 功能关闭时说「待删除」是假声明（N234）。
            #
            # M86：改走 Ui 通道。json 模式下它进 events（**信息不丢**），
            # 文本模式输出逐字不变 —— 比"静音"好：静音会让 json 消费者
            # 完全看不到这一句，而它说的是"还有 N 个路径远端留着副本"。
            _ui_now = get_ui()

            def _say(t):
                if _ui_now is not None:
                    _ui_now.line(t)
                else:
                    print(t)

            _say(f"     {len(rest)} 个路径远端仍保留副本"
                 f"（删除功能关闭，本脚本不会处理它们）：")
            for _p in rest[:20]:
                _say(f"       - {_p}")
            if len(rest) > 20:
                _say(f"       … 另有 {len(rest) - 20} 个")
            _say("     这些路径本地已不存在，日后不会再被任何本地改动触发；"
                 "需手工清理，或启用删除支持后按提示处理。")


def baseline_changed_files(state, lmap):
    """本地与**基线**不同的文件 —— 补上 git status 看不见的「已 commit 未推送」。

    detect_changes() 的对比基准是本地 HEAD，用户一旦 `git commit`，
    工作区变干净，git status 就报不出任何东西，改动永远推不上去且**静默失败**。

    这里改用基线（上次推送/同步后记下的状态）做基准：本地 ≠ 基线 说明
    这个文件在你这儿变过（改过、或合并过远端改动），就该纳入推送候选。

    刻意**不**用远端做基准：远端被别人改了但你没动过时，本地 == 基线，
    这条不会把它算进来 —— 于是你不会拿旧版本去覆盖别人的新改动。
    「远端被改过」由第一层校验负责拦，两件事分开。

    基线里没记录的文件**也要纳入**：lmap 取自 git 索引，未跟踪文件不在
    其中，所以「在 lmap 里却不在基线里」= 本地新增且已 commit 的文件。

    原先这里直接 continue，理由是「归第一层的基线未记录分支处理」。
    但那个分支只作用于**已经进入待推集合**的文件：工作区因 commit 而
    干净时 detect_changes 为空、这里又跳过，文件根本到不了第一层 ——
    实测新增文件 commit 后永远推不上去，且零报错（本仓库的
    test_runtime.py 就是这么漏掉的）。文档声明的兜底分支收不到它。

    纳入候选后由第一层按「远端有没有」决定：
      远端没有 → 纯新增，推
      远端有   → 无法确认远端原状态，拦下（fail-closed）

    基线整体为空时不推断：那说明根本没建过基线，把整个仓库当新增推出去
    是另一种事故。那种情况由 P0-1 / --init-baseline 管。
    """
    out = []
    baseline = state.get("files") or {}
    for rel, lmode_lsha in lmap.items():
        base = baseline.get(rel)
        if not isinstance(base, dict) or not base.get("sha"):
            if baseline:
                out.append(rel)
            continue
        if lmode_lsha != (base.get("mode"), base.get("sha")):
            out.append(rel)
    return out


def _merge_base_coverage(prev_state, paths):
    """这些文件在**旧基线**里有没有可信的合并基点。

    返回 (有基点的, 没基点的)。--pull 只对前者给出可信的三方合并。

    判据不是「有没有基线文件」，而是「能不能取到可信的合并基点」——
    这两者不是一回事，分叉场景正是它们分道扬镳的地方：

      · 旧基线记了该文件上次同步时的远端 sha → **可信**。
        它回答的是「本地副本当时基于哪个远端版本」，本地之后怎么改都不影响
        它作为共同祖先的地位。远端单方面前进、两边真分叉，都能靠它正确三方合并。

      · 没有 → _base_bytes() 会回退到本地 git HEAD 那一档，而 HEAD 在用户
        `git commit` 之后就**含了他自己的改动**。拿它当 base 等于宣称
        「用户加的那行是共同祖先的一部分」，于是「远端没有这行」会被判成
        「远端故意删掉了」，三方合并**干净通过、零冲突** —— 用户改动静默消失。

    所以缺省那句「先跑 --pull」在没基点时是**错的出路**：它不崩、不拦，
    只是安静地用一个错的基点合并完。比不给提示更糟。
    """
    covered, uncovered = [], []
    prev_files = ((prev_state or {}).get("files") or {})
    for p_ in paths:
        e = prev_files.get(p_)
        sha = e.get("sha") if isinstance(e, dict) else e
        (covered if sha else uncovered).append(p_)
    return covered, uncovered


def init_baseline(head_sha, rstate):
    """把远端当前状态记为基线。

    这一步本质是「无条件信任远端当前状态」，无法区分差异是别人改的还是你改的，
    所以「本地 ≠ 远端」的文件**不给它记基线**——留空后第一层校验会把它判为
    「基线未记录、远端却已存在」而拦住，强制先同步再推。

    （旧版做法：照样记基线、照样把 synced_commit 设成 head_sha，只打印一行警告。
     结果是本地旧副本的全文 + 你的改动会被整文件覆盖上去，把远端更新静默回退掉，
     全程零报错——正是本脚本最想防的场景，却被自己的初始化开了后门。）
    """
    lmap = local_state_map()
    # 只有**内容**不同才算风险：内容一致、仅 mode 不同（容器里文件常一律 0777，
    # 索引 mode 与远端不同但字节完全相同）推上去不会丢任何东西。
    # 若把 mode 差异也算进 diff，这类环境首次 init 会把整个仓库判成「不一致」，
    # 全部拒绝建基线，人看了只会困惑。
    diff = sorted(p for p in rstate if p in lmap and lmap[p][1] != rstate[p][1])
    mode_only = sorted(p for p in rstate if p in lmap
                       and lmap[p][1] == rstate[p][1] and lmap[p][0] != rstate[p][0])
    # 远端有、本地没有。不算 diff（否则全新目录首次 init 会被 P0-1 永久拦住，
    # 用户只想推自己的改动时不该被挡），但**必须明确提示**：
    # 这些文件此前会被一路静默忽略，表现为「本地缺一批文件且 --pull 拉不下来」。
    missing = sorted(p for p in rstate if p not in lmap)
    # A4-H3：`set(diff)` 写在推导式的条件里 → **每个 p 都重建一次集合**，
    # 重建次数 = 远端文件总数。实测 10000 文件 / 2000 diff 时 0.135 秒，
    # 提到循环外是 0.001 秒（135×）。
    _diff_set = set(diff)
    files = {p: {"mode": m, "sha": s} for p, (m, s) in rstate.items()
             if p not in _diff_set}

    # 保留既有 workflow：--direct 用户跑 --reset-baseline 不该被静默改回 PR。
    # 读当前基线（若存在）；不存在的（首次初始化）才用默认值。
    # 旧基线要先读、且**在 save_state 覆盖之前**读：
    #   · 保留既有 workflow（--direct 用户跑 --reset-baseline 不该被静默改回 PR）
    #   · 判断 --pull 有没有可信的合并基点（见下面 diff 分支的两套文案）
    prev_state = None
    try:
        _prev = _load_state_strict()
        if isinstance(_prev, dict):
            prev_state = _prev
    except BaseException:
        prev_state = None
    prev_workflow = (prev_state or {}).get("workflow")          # 旧基线读不了就走默认，不阻断重建

    state = {
        "version": STATE_VERSION,
        "remote": f"{OWNER}/{REPO}",
        "branch": BRANCH,
        "base_commit": head_sha,
        # 本地副本基于哪个远端 commit —— P0-1 的过期校验靠它。
        # 有 diff 时**必须留空**：本地明显不等于远端，声称「已同步到 head_sha」
        # 是假声明，会让过期防护整体失效。留空后 P0-1 一律拦（fail-closed）。
        "synced_commit": None if diff else head_sha,
        # N98：--reset-baseline 连带清空 tasks / task_branch / pr_number。
        # 实测：用户正用 --hold 攒着一条任务分支（分支上有已推送的改动），
        # reset 之后 tasks 空了、task_branch 变 None —— 那条分支从此
        # 既不在本地记录里、也没有 PR，成为只能靠 --prune 偶然发现的孤儿。
        #
        # --reset-baseline 的语义是重建**文件基线**，不是放弃进行中的任务。
        # 这两件事被捆在一起，而提示里只说了"重建基线"。
        #
        # 保留任务上下文分支，但只在**同名仓库**时保留：
        # reset 到另一个仓库时沿用旧任务记录会指向不存在的分支。
        # N228：tasks / task_branch 在 N129 加了 remote 判据（跨仓库不沿用），
        # 但 conflicts 与 pr_number 同样属于「进行中的状态」，没加 ——
        # 换仓库 init 时会把**上一个仓库**的冲突清单与 PR 号带过来：
        #   · conflicts 非空 → 未解决冲突 Guard 拦下本次推送，
        #     而那些文件属于别的仓库，本地根本解决不了
        #   · pr_number 指向别的仓库的 PR → --merge 合并错对象
        # 与 tasks 同一判据。
        "conflicts": ([] if not prev_state or
                      (prev_state.get("remote") not in (None, f"{OWNER}/{REPO}"))
                      else list(prev_state.get("conflicts") or [])),
        "tasks": ({} if not prev_state or
                  (prev_state.get("remote") not in (None, f"{OWNER}/{REPO}"))
                  else dict(prev_state.get("tasks") or {})),
        "task_branch": (None if not prev_state or
                        (prev_state.get("remote") not in (None, f"{OWNER}/{REPO}"))
                        else prev_state.get("task_branch")),
        # 不能写死默认值：--direct 用户跑 --reset-baseline 后会被静默
        # 改回 PR 工作流，下次推送走的是另一套逻辑且没有任何提示 ——
        # 静默改变行为，用户要等到出事才发现。
        "workflow": (prev_workflow if prev_workflow else DEFAULT_WORKFLOW),
        # N98 续：pr_number 同样属于"进行中的任务"，不该被 reset 清掉。
        # 清掉后 --merge 拿不到号，而分支还在远端 —— 用户会以为没有 PR。
        "pr_number": (None if not prev_state or
                      (prev_state.get("remote") not in (None, f"{OWNER}/{REPO}"))
                      else prev_state.get("pr_number")),
        "files": files,
        # local_seen：**必须由主程序自己写**，不能依赖插件。
        # 插件默认关闭（防任意代码执行），而 local_seen 是删除检测的
        # 第三道判据 —— 插件不在就没人维护它，实测 init 后它是空列表，
        # 于是删除检测永久失效：本地删了文件，远端留着，脚本从不提示。
        #
        # 语义是「本地曾经拥有过」（只增不减），init 时 = 基线全量。
        # 基线路径都来自本地同步，说它们「本地曾经有过」与事实相符。
        #
        # N238：**不成立**。基线里混着「远端独有、本地从未有过」的文件
        # （init 是从远端树建的基线，那些文件本地根本没有）。
        # 把它们写进 local_seen，等于让判据 3 对它们恒真 ——
        # 而 collect_removal_candidates 明确写着：
        #   「不能对 seen 里没有的候选做基线兜底，会把别人的文件
        #     当成本地删掉的」
        # 于是这些远端独有的文件日后会被**误删**（用户从未拥有过它们）。
        # 三处代码各说各话，这是唯一会真删错文件的一条。
        #
        # 正确口径：只写「本地当前确实有」的路径。
        # 代价：远端独有的文件进不了 local_seen，日后若被删
        # 不会进待删除清单（漏报）—— 漏报比误删安全得多。
        # M17：`os.path.exists` 对悬空软链返回 False，于是它进不了
        # local_seen → 日后真删它时静默**漏删**（安全侧，但仍是缺陷）。
        # 统一走 _local_exists（lexists）。
        "local_seen": sorted(p for p in files
                             if _path_json_safe(p)
                             and _local_exists(p)),
        # 必须在这里就写：init 的语义就是「声明当前本地副本对应远端
        # head_sha」，而 local_head 正是这道声明的**本地一侧**。
        #
        # 漏写的后果（实测）：新仓库首次 --init-baseline 后立刻推送 ——
        # 这是最常见的一条路径 —— 会看到
        #   「状态文件里没有 local_head，本地回退检测暂不生效」
        # 于是这道 P0 防护恰好在「首次使用、最易误操作」的时刻是关着的。
        # 代码明知有洞却只提示不拦，等于把防护推迟到「下次同步成功之后」，
        # 而用户完全不知道自己此刻没有防护。
        #
        # 与 synced_commit 不同，local_head **无条件写**：即便有 diff
        # （synced_commit 留空表示「不能声称已同步」），本地 HEAD 是什么
        # 仍是事实，记下来只会让回退检测更早生效，不会放松任何一道校验。
        "local_head": _local_head(),
        # N381（第一百二十二轮，高危）：把「远端独有、本地从未有过」
        # 这个事实**持久化**下来 —— init_baseline 是本脚本唯一确切知道
        # 这件事的时刻（它刚比对过远端树与本地 lmap）。事后无法再推断：
        # 克隆仓库里本地 main 与 origin/main 是同一条提交链，别人在主干
        # 上的「加 x → 删 x」全在这条链上，于是被当成用户自己的删除
        # → 判据 3 对 x.txt 恒真 → **真删远端独有的文件**。
        # 代价：记为远端独有的文件日后即使用户拉下来又删掉也不会进
        # 待删除清单（漏报）—— 漏报是 N45 论证过的安全方向。
        "remote_only": sorted(p for p in missing if _path_json_safe(p)),
    }
    # local_seen：见 mark_synced 处的说明，与基线同点更新。
    _sync_local_seen(state, lmap) if _sync_local_seen else None
    save_state(state)
    # M86：`--init-baseline --json` 的**成功**路径也要给 JSON。
    # 只修失败路径不够 —— 消费者拿到纯文本 + rc=0，一样不知道成了没有。
    if _json_active():
        print(json.dumps({
            "ok": True, "baseline": True, "reason": "baseline_written",
            "action": "init_baseline",
            "state_path": STATE_PATH,
            "files": len(files),
            "base_commit": str(state.get("base_commit")),
            "missing": len(missing),
        }, ensure_ascii=False))
        _mark_json_emitted()
    else:
        print(f"✅ 基线已写入 {STATE_PATH}（记录 {len(files)} 个文件，未做任何推送）")

    # M86：json 模式下这一段（以及下面的 mode_only / diff）是**补充说明**，
    # 数量已经进了上面那个 JSON 的 `missing` 字段。留着它们会让
    # `--init-baseline --json` 整体不可解析 —— 而那正是 M86 要修的东西。
    # 只在**非** json 模式下打印，文本输出逐字不变。
    if missing and not _json_active():
        print(f"\n  · 本地缺少 {len(missing)} 个远端存在的文件"
              f"（基线已按远端记录，不影响你推送自己的改动）：")
        for p in missing[:10]:
            print(f"   - {p}")
        if len(missing) > 10:
            print(f"   … 其余 {len(missing) - 10} 个")
        # 被 .gitignore 屏蔽的那部分**不能**推荐 --pull：
        # 拉下来本地 git 照样不跟踪它，下次检测还是看不见 —— 出路本身
        # 走不通。而且它们多半是运行时产物（审查进度、构建缓存），
        # 拉一份到本地毫无意义。
        miss_ignored = sorted(gitignored_set(missing))
        if miss_ignored:
            print(f"\n   其中 {len(miss_ignored)} 个被 .gitignore 屏蔽，"
                  f"本地 git 不跟踪，--pull 也拿不回来：")
            for p in miss_ignored[:5]:
                print(f"   - {p}")
            if len(miss_ignored) > 5:
                print(f"   … 其余 {len(miss_ignored) - 5} 个")
            print("   它们通常是运行时产物；远端那一份由谁维护，"
                  "推之前先确认，别用本地快照覆盖。")
        # 同 A4-H3（第二处）：`set(miss_ignored)` 在推导式内同样是每元素重建。
        _miss_ignored_set = set(miss_ignored)
        rest = [p for p in missing if p not in _miss_ignored_set]
        if rest:
            print("    需要把这些文件拿到本地，跑：python3 push_api.py --pull")

    if mode_only and not _json_active():
        print(f"\n  · {len(mode_only)} 个文件内容一致但权限位与远端不同"
              f"（按远端 mode 记入基线；推送时会把本地权限带上去）：")
        for p in mode_only[:10]:
            print(f"   - {p} 本地 {lmap[p][0]} / 远端 {rstate[p][0]}")
        if len(mode_only) > 10:
            print(f"   … 其余 {len(mode_only) - 10} 个")

    if diff and not _json_active():
        print(f"\n❌ 本地与远端不一致的文件 {len(diff)} 个：已**拒绝**为它们建立基线。")
        for p in diff[:20]:
            print(f"   - {p}")
        if len(diff) > 20:
            print(f"   … 其余 {len(diff) - 20} 个")
        print("\n   这些文件本地与远端不同，无法判断差异属于谁。直接推送会把远端内容")
        print("   整文件覆盖掉且不报错。")

        covered, uncovered = _merge_base_coverage(prev_state, diff)
        if covered and not uncovered:
            # 有可信基点：--pull 就是正解（无论「远端单方面前进」还是「两边真分叉」，
            # 共同祖先都是基线记的那个版本，三方合并都成立）。
            print("\n   先合并远端改动：\n")
            print("     python3 push_api.py --pull        # 三方合并远端改动到本地\n")
            print("   合并完成、本地与远端一致后，本脚本会自动补齐基线。")
        else:
            # 没有可信基点 —— 分叉（或首次初始化）场景。此处 --pull **不成立**。
            if uncovered:
                print(f"\n   其中 {len(uncovered)} 个在旧基线里没有记录，"
                      f"--pull 拿不到可信的合并基点：")
                for p_ in uncovered[:10]:
                    print(f"     - {p_}")
                if len(uncovered) > 10:
                    print(f"     … 其余 {len(uncovered) - 10} 个")
            print("\n   ⚠️  此时不要跑 --pull：没有可信基点，它会退回用本地 git HEAD")
            print("     当合并基点。后果取决于你的改动 commit 过没有（两种都实测过）：\n")
            print("       · 已 commit 过改动、工作区**还有**未提交的改动")
            print("         → HEAD 里含你已 commit 的内容，被当成共同祖先的一部分；")
            print("           「远端没有它」会被判成「远端故意删掉了」，于是你**已 commit")
            print("           的那部分改动静默消失**。零冲突、无提示，文件看着是合并好了。\n")
            print("       · 已 commit 过改动、工作区干净")
            print("         → 基点退化成「空内容」，两侧都被当新增，报出来的冲突")
            print("           **未必是真冲突**（文件被塞满冲突标记，其实远端可能只改一行）。\n")
            print("     两种都不崩、不拦。丢的是你自己的改动，不是远端那一份 ——")
            print("     方向别搞反，否则会按错误的优先级去核对。")
            print("\n   要人工确定「两边是从哪个版本分出去的」，用那个版本做三方合并：\n")
            print("     git merge-file -p 你的版本 共同祖先版本 远端版本 > 合并结果\n")
            print("   共同祖先通常是：另一台机器上同步的同名文件 / 更早的备份 /")
            print("   本次任务最初拿到的那份。确定不了就先只推你改动的文件（点名推送），")
            print("   把这几个文件的冲突留到人工处理。")
            # 实测：--reset-baseline 对 diff 文件是**留空**而非记成远端当前状态，
            # 第一层因此判「基线未记录、远端却已存在」而拦住。旧文案说它
            # 「放行覆盖」是错的 —— 把安全的行为写成危险的，同样误导。
            print("\n   · 这几个文件的基线已**留空**（不是记成远端当前状态）：")
            print("     后续推送会被第一层拦住，直到合并完、本地与远端一致 —— 这是有意的。")
            print("     真正会放行覆盖的是 --force-overwrite / --force-file，别用它们绕过。")
            if covered:
                print(f"\n   （其中 {len(covered)} 个旧基线里有记录，那几个可以用 --pull 合并。）")


def mark_synced(state, head_sha, rstate):
    """标记「本地副本已同步到远端 head_sha」，解除 P0-1 的过期拦截。

    顺带刷新「本地已与远端一致」的条目基线，避免同步后仍报旧的差异。

    有**未合并的远端改动**时拒绝标记：远端变过、本地又还没合上，此时声称
    「已同步到 head_sha」是假声明，P0-1 一旦放行，本地旧内容就会被整文件
    覆盖上去，把远端更新静默回退掉。先跑 `--pull` 再标记。
    """
    # 有未解决冲突时**不能**声明已同步。
    #
    # mark_synced 是把 P0-1（本地副本过期）这道闸门解除掉。而冲突文件的
    # 本地内容此刻是带冲突标记的半成品 —— 解除闸门后推送，等于允许把
    # 「本地旧版本 + 冲突标记」盖到远端上去。
    # 冲突拦截与 P0-1 是两道独立的闸门，这一处不能替另一处放行。
    # P0：--mark-synced 的守卫只认「远端变过 + 本地没合上」，不认「本地退了」。
    # 于是本地回退后它能照常解除 P0-1，v2 静默覆盖主干 v3（实测确认）。
    # 与 pull() 那两处是同一语义的三份实现，这里不能漏。
    if _refuse_if_rewound(state, "--mark-synced",
                          "   恢复后重跑 --mark-synced。"):
        _blocked("已取消标记（本地副本状态存疑，未解除过期拦截）。")

    pending_conflicts = list(state.get("conflicts") or [])
    if pending_conflicts:
        # M154（A143，中危）：N374 ③ 把「本地已删除」的出口（`--resolve --yes`）
        # 修在了 `UnresolvedConflictGuard` 上 —— 而**默认配置（删除关闭）下
        # 推送根本不拦已删的冲突文件**（删除不进待推集合 → Guard 放行、rc=0、
        # 零提示，A143 §3.1 实测）。用户真正撞到墙的地方是**本处**
        #（`--mark-synced`），它一直没有 `_here` / `_gone` 分流，
        # 与 Guard 不同源。
        #
        # A93 记录的原始困境是"四条出路没有一条能清"；N374 ③ 修通的那条
        # 只在推送被拦时显示，而推送在默认配置下不拦 ——
        # **出口修在了用户看不到的地方，看得到的地方没修**。
        # 这里按同一口径（`_local_exists`，"查不到"按"还在"处理）分流。
        _gone_m = [r for r in pending_conflicts if not _local_exists(r)]
        # A4-H3 同族：集合提到推导式外一次建好，不在逐元素判定时重建。
        _gone_m_s = set(_gone_m)
        _here_m = [r for r in pending_conflicts if r not in _gone_m_s]
        _lines_m = [
            f"  冲突文件内容尚未定稿，此时解除过期拦截会让它被推上远端。"]
        if _here_m:
            _lines_m.append(
                f"  手工改好后逐个运行：python3 push_api.py --resolve <文件>"
                f"\n    待改：{_here_m[:10]}")
        if _gone_m:
            _lines_m.append(
                f"  本地已删除（放弃改动、保留远端那份）的文件，"
                f"--resolve 需要 --yes：\n"
                f"    python3 push_api.py --resolve {_gone_m[0]} --yes"
                + (f"\n    同类共 {len(_gone_m)} 个：{_gone_m[:10]}"
                   if len(_gone_m) > 1 else ""))
        _blocked(
            f"还有 {len(pending_conflicts)} 个未解决的冲突，不能标记已同步："
            f"{pending_conflicts[:10]}\n"
            + "\n".join(_lines_m))

    lmap = local_state_map()
    refreshed = 0
    unmerged = []
    for p, (rmode, rsha) in rstate.items():
        lm = lmap.get(p)
        base = state["files"].get(p)
        base_sha = base.get("sha") if isinstance(base, dict) else base
        # N422（第一百三十六轮，中危）：第二个条件原本只比 sha，
        # `rmode` 取出来了却没用在「远端变过吗」这一问上 ——
        # 「远端只改了可执行位、内容一字未变」这整类变更恒等于"没变过"。
        #
        # 决定性对照（同一份状态、唯一变量是远端改的是 mode 还是内容）：
        #   远端 (100755,X) 基线 (100644,X) 本地 (100644,X)
        #     → 放行，"✅ 已标记本地副本基于远端"，synced_commit 写入  ★
        #   远端 (100644,Y) 基线 (100644,X) 本地 (100644,X)
        #     → 拦下，synced_commit 保持 0
        # **同一个「本地还没合上远端改动」的事实，改内容就拦、改权限位就放行。**
        #
        # 同一语义在本文件里共三份实现，另两份（pull() 的 cand、
        # FileBaselineGuard.check）**都含 mode** —— 漏的正是这份注释
        # 自称"不能漏"的那一份。改成与它们同源。
        base_mode = base.get("mode") if isinstance(base, dict) else None
        if lm != (rmode, rsha) and (rmode, rsha) != (base_mode, base_sha):
            unmerged.append(p)          # 远端变过 + 本地没合上
            continue
        if lm != (rmode, rsha):
            continue                    # 远端没变，差异纯粹是你自己的改动
        # M138（A127，观察项）：这里原先自带一份判据
        #   `not isinstance(base, dict) or base.get("sha") != rsha
        #    or base.get("mode") != rmode`
        # —— 与 `_baseline_stale()` **逐用例等价**（A127 实测 7/7 一致），
        # 但它是第二份实现。而 `_baseline_stale` 的 docstring 白纸黑字
        # 写着一个**写死的计数**（声称刷新点一共就那么几处，抽成同一份
        # 判据后"不再靠谁想起来补谁"）—— 第五处正是这里，却没被收敛进去。
        # （那个计数本身也是错的，见 N583：两边各数各的，实际多出一处。）
        #
        # 与 M98（`_lbytes` vs `_local_bytes`）同族：当前等价，所以不定级；
        # 但两份实现会静默漂成两套口径（比如有人把 `None` 的处理改掉），
        # 且没有任何测试覆盖两者的一致性。收敛是零风险改动。
        if _baseline_stale(base, rmode, rsha):
            state["files"][p] = {"mode": rmode, "sha": rsha}
            refreshed += 1

    if unmerged:
        # N423（第一百三十六轮，中危）：这里原本是 `print` 后 `return`，
        # 而同一函数另外两条拒绝路径（本地回退 / 有未解决冲突）都用
        # `_blocked()`。顶层只认 `_LAST_RUN_BLOCKED`(2) 与
        # `_LAST_RUN_CANCELLED`(4)，这条两个都不置位 → **退出码 0**。
        # 而脚本刚刚**明确拒绝**了这次声明 —— CI 拿到 0 会以为已同步。
        # 与已修的 N365 完全同族。
        _blocked(
            f"还有 {len(unmerged)} 个文件的远端改动未合并进本地，不能标记已同步："
            f"{unmerged[:20]}\n"
            f"   先合并再声明同步：\n"
            f"     python3 push_api.py --pull      # 三方合并远端改动到本地\n"
            f"   合并后再跑 --mark-synced。")

    state["synced_commit"] = head_sha
    state["local_head"] = _local_head()
    state["base_commit"] = head_sha
    # local_seen：记录「本地曾经拥有过哪些路径」，删除功能的第三道判据。
    # 必须与基线刷新同点调用 —— 顺序反了会让刚推上去的文件在基线里
    # 却不在 local_seen 里，日后被删时判成「远端独有」而漏删。
    _sync_local_seen(state, lmap) if _sync_local_seen else None
    save_state(state)
    print(f"✅ 已标记本地副本基于远端 {head_sha[:8]}（刷新 {refreshed} 条已一致的基线）")


# ---------------------------------------------------------------- 拉取与三方合并

def _remote_bytes(sha):
    """取远端 blob 原始字节。用 raw 而非 JSON：JSON 形式对 >1MB 不返回 content。"""
    try:
        return api("GET", f"/git/blobs/{sha}", raw=True,
                   timeout=_timeout_for(MAX_BLOB_BYTES) + 60)
    except SystemExit as e:
        print(f"  ! 取远端 blob {str(sha)[:8]} 失败：{e}")
        return None


def _local_bytes(rel):
    full = os.path.join(ROOT, rel)
    if os.path.islink(full):
        return os.readlink(full).encode("utf-8", "surrogateescape")
    try:
        with open(full, "rb") as f:
            return f.read()
    except OSError:
        return None


def _binary(b):
    return b is not None and b"\0" in b[:64 * 1024]


def _merge3(local, base, remote):
    """三方合并，返回 (合并后字节, 是否冲突)。

    二进制不做合并——git merge-file 只按行处理，对二进制产出的结果是垃圾。
    直接判冲突，交给人工：本地/远端副本都会另存出来。
    """
    if _binary(local) or _binary(base) or _binary(remote):
        return None, True
    paths = []
    try:
        for data in (local, base, remote):
            fd = tempfile.NamedTemporaryFile(prefix="merge3-", delete=False)
            fd.write(data or b"")
            fd.close()
            paths.append(fd.name)
        # N100（**已修**，此处改过去时）：这里**曾经**是全脚本唯一没接
        # TimeoutExpired / OSError 的 subprocess.run 调用点。
        # 有 timeout= 却不接超时异常，大文件合并超时会冒成裸 traceback
        # —— 而 --pull 正跑到一半，用户看到堆栈会以为合并把文件搞坏了。
        # 另外 OSError（git 不在 PATH / 临时目录不可执行）同样**曾经**没接。
        #
        # 现状：下面两个 except 都已接住（TimeoutExpired + OSError/
        # SubprocessError），本处不再属于"没接"的那一类。
        #
        # A88（第 88 轮）复核时发现两件事，一并记在这里：
        #  ① 注释里的"19 处"是**当时的**数字，现为 25 处。
        #     修 N100 时只改了代码，没改这个数字 —— 注释数字过时与
        #     注释不跟代码改是同一类，只是这处连语气都没换成过去时，
        #     于是与紧邻下方的两个 except 自相矛盾（与 A3-G2 同族）。
        #  ② 仍有多处只接 TimeoutExpired 不接 OSError：_curl_once 内
        #     4 处（两类都不接）、另有 5 处只接 TimeoutExpired。
        #     **实测**（PATH 里不放 curl/git）：退出码 1 + 完整 traceback，
        #     是"会响"的失败不是静默 —— 故定 P3 未修。
        #     注意 OSError ⊃ FileNotFoundError，TimeoutExpired 是
        #     SubprocessError 的子类，两者不能互相替代。
        #
        # N564（第一百八十四轮，低危）：上面原先写"3 处"，AST 扫出来是
        # **4 处** —— 第 4 处是 `except BaseException: …raise`（捕获后
        # 原样抛出，同样两类都不接，只是多清理了一个临时文件）。
        # 写死数字的代价就是每新增一处它就过期一次，**而过期后看着
        # 仍然像在描述全部**（与"闸门 docstring 写死三处调用点，实际
        # 5 处"同族）。
        #
        # 另：②的二分法（"没接" / "接了"）漏了第三类 —— **捕获后静默
        # 给出占位值**（`_stash_hint` 的 `return ""`、`_git_blob_bytes`
        # 的 `_size = 0`）。这一类恰恰是"不是静默"那句定级的反例
        #（见 N563），已分别修掉。
        try:
            out = subprocess.run(
                ["git", "merge-file", "-p",
                 "-L", "本地", "-L", "基线", "-L", "远端", *paths],
                capture_output=True, timeout=DEFAULT_GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(f"  ! git merge-file 超时（>{DEFAULT_GIT_TIMEOUT}s，"
                  f"文件过大？）：本次按冲突处理，请手工解决。")
            return None, True
        except (OSError, subprocess.SubprocessError) as _e:
            print(f"  ! git merge-file 无法执行（{type(_e).__name__}）："
                  f"本次按冲突处理，请手工解决。")
            return None, True
        if out.returncode < 0 or (out.returncode > 0 and not out.stdout):
            return None, True
        return out.stdout, out.returncode != 0
    finally:
        for p in paths:
            # 必须吞掉：finally 里抛出的异常会**替换**掉 try 中正在传播的
            # 真实异常。届时用户看到「删除临时文件失败」，而真正的原因
            #（合并失败/冲突）被吞掉 —— 排查方向完全错。
            try:
                os.unlink(p)
            except OSError:
                pass


def _conflict_marker_hit(data, strict=False):
    """查冲突标记，返回 **(是否有, 命中的那一行原文)**。

    N288（第八十四轮，中危）：本函数**完全不认 `=======`** ——
    于是 N225 要防的事故重新静默：

      ours 内容
      =======          ← 用户手工解决时只删了头尾，中间这段还在
      theirs 内容

    `--resolve` 接受它 → 基线补成远端值 → 下次推送把残缺内容推上去。

    这个取舍当初是**被迫**的（N233）：把 `=======` 当裸子串查会误伤
    5 类正常文件（Python 注释分隔线 / Markdown setext 标题 /
    YAML+ini 分隔线 / 对齐等号串 / diff 分隔线），
    后果是 `--resolve` 拒绝 → conflicts 永不清除 → **推送永久阻塞**，
    且 `--force-overwrite` 对冲突文件不适用 —— 无出路的死锁。

    **破局点在上下文**（审查报告给出的思路，我认为是对的）：
    两个调用点都**已知这个文件是有冲突史的** ——
      · `resolve()`：进入判据前先过 `if rel not in state["conflicts"]`
      · `clean_conflict_artifacts()`：pending 直接来自 `state["conflicts"]`
    于是"已知冲突的文件里出现行首 `=======`"与"Markdown 文件里出现
    行首 `=======`"**不是同一件事** —— 前者有冲突史，后者没有。
    原 docstring 那句"无法区分"在已知冲突的场景下不成立。

    所以：`strict=True` 时额外认行首 `=======`。
    `strict=False`（默认）保持原行为，供不确定上下文的调用方使用。

    再把命中的**那一行原文**返回去：即便真误判，用户能看见是哪一行、
    自己删掉或改名 —— 这就打破了 N233 那个"无出路的永久阻塞"。
    """
    if not data:
        return False, None
    try:
        text = data.decode("utf-8", "surrogateescape")
    except (UnicodeDecodeError, LookupError):
        return False, None
    for ln in text.splitlines():
        if ln.startswith("<<<<<<<") or ln.startswith(">>>>>>>"):
            return True, ln
        # M87：必须是**恰好 7 个等号**（独占一行），不能是 startswith。
        # 实测真例：delete_rename_support.py 943 行里有大量 76 个等号的
        # 装饰性分隔线 —— 该文件一旦进 conflicts 就永远无法 --resolve
        #（每次都报「仍存在冲突标记」），**推送永久阻塞**。
        #
        # 为什么不是「必须与 <<<<<<< / >>>>>>> 成对出现」：N225 的场景
        # 恰恰是只剩中间的 =======（头尾被手工删掉），那时文件里没有
        # 任何 <<</>>>，要求成对会把它判成已解决 —— 残缺内容被推上去，
        # 正是 N288 要堵的事故。
        if strict and ln.rstrip() == "=======":
            return True, ln
    return False, None


def _has_conflict_markers(data, strict=False):
    """判断内容里是否残留 git merge-file 的冲突标记。

    N233：不能按**裸子串**查 —— `=======` 会命中 Python 注释分隔线、
    Markdown setext 标题、YAML 分隔线等 5 类正常文件，
    把它们判成「仍有冲突标记」→ --resolve 拒绝 → 推送永久阻塞。

    git merge-file 生成的标记必定**独占一行且在行首**：
            
    M16（A15 / A24）：上面这句"**配对出现**才算冲突"与实现**不符** ——
    实现是 `has_start or has_end`（任一出现即算）。

    哪个对？**实现对**。判据要的是"内容是否残留冲突"，
    而"只删了 >>>>>>>、<<<<<<< 还在"同样是残留 ——
    按"配对"判会把它当成已解决（fail-open），恰恰是该拦的那类。
    所以按 fail-closed 取"任一"。

    注释写"配对"会诱导下一位维护者把 `or` "修正"成 `and`。
    改注释而不是改实现。
    """
    # 约定：默认只看行首的 <<<<<<< / >>>>>>>，不看 =======。
    # ======= 单独出现是否算冲突，取决于**这个文件有没有冲突史** ——
    # 那是 strict 开关的事，详见 _conflict_marker_hit 的说明。
    #
    # N288：实现收敛到 _conflict_marker_hit —— 同一份判据，
    # 不再有第二份循环（原先两份循环改一处漏一处）。strict 透传。
    return _conflict_marker_hit(data, strict=strict)[0]


def _write_local(rel, data, mode):
    full = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(full) or ROOT, exist_ok=True)
    if os.path.islink(full):
        os.unlink(full)
    if mode == "120000":
        # lexists 而非 exists：exists 对**悬空软链**返回 False，
        # 于是 os.symlink 会因为目标已存在而抛 FileExistsError。
        # 上面 if islink 那行负责删软链，这里处理的是它之后的残留情况。
        if os.path.lexists(full):
            os.unlink(full)
        # **不能 strip**。
        #
        # symlink 的 blob 内容就是目标路径字符串本身，远端存什么就写什么。
        # strip 掉末尾换行后：远端 blob b'/opt/target\n' 的 sha 是
        # cfc33591，本地写入 '/opt/target' 再读回算出的 sha 是 05578718
        # —— 两者永久不等。
        #
        # 后果与已修的 autocrlf 分叉同症状：第一层校验永久报「可能被他人
        # 改动」，而根本没人改过，最终把用户推向 --force-overwrite 或
        # --reset-baseline。归一化要做在**比对侧**，不能做在写入侧。
        target = data.decode("utf-8", "surrogateescape")
        # 校验用 strip 后的值：路径语义上首尾空白无意义，
        # 但落盘必须保留原字节（否则 sha 对不上）。
        _check = target.strip()
        # 远端给的目标**必须校验**：safe_rel() 只防了**读取**侧，写侧没有。
        # 不校验的话，远端（或仓库被投毒时）给出 `../../etc/passwd`
        # 就会在落盘时被原样创建 —— 之后任何读它的操作都被带到仓库外。
        # 同一份代码里读侧防了、写侧没防，是最容易被忽略的不对称。
        #
        # 绝对路径一律拒绝：symlink 到仓库外的固定路径（/etc、/home）
        # 在推送/读取时都会造成越界，没有合法用途。
        if os.path.isabs(_check):
            _blocked(
                f"{rel} 是符号链接，目标 {_check!r} 是绝对路径，拒绝创建。\n"
                f"  仓库内不应出现指向绝对位置的软链；请确认远端内容是否可信。")
        # 相对目标：解析后必须仍在仓库内。
        # 判据走 _inside_root()（基准 = realpath(ROOT)），与 safe_rel 同源：
        # 早先这里直接拿裸 ROOT 比，仓库根是软链时会被判成越界而 SystemExit，
        # 中断整个 --pull（实测复现）。
        resolved = os.path.realpath(os.path.join(os.path.dirname(full), _check))
        if not _inside_root(resolved):
            _blocked(
                f"{rel} 是符号链接，目标 {_check!r} 解析后指向仓库外：{resolved}\n"
                f"  拒绝创建（防止路径穿越）。")
        os.symlink(target, full)
        return
    # 同样用 lexists：exists 对悬空软链返回 False，会让「软链被普通文件
    # 覆盖」这种情况绕过这里的类型检查。
    if os.path.lexists(full) and not os.path.isfile(full):
        _blocked(f"{rel} 不是普通文件，拒绝覆盖（请手工处理）")
    with open(full, "wb") as f:
        f.write(data)
    if mode == "100755":
        os.chmod(full, 0o755)
    else:
        # 非执行文件必须显式回退权限位。
        #
        # 不然会与 pull 永久打架：远端文件从可执行改成不可执行时，
        # 本地文件若是 0755（之前 chmod 过），写入内容后权限位仍是 0755，
        # local_state_map 算出的 mode 就是 100755 ≠ 远端 100644
        # → 每次都判定「本地与远端不同」→ 每次 pull 都重写 → 永远合不完。
        # 只写内容不写权限位，等于只同步了一半状态。
        try:
            os.chmod(full, 0o644)
        except OSError:
            pass                      # 权限改不动不该阻断写入
    # 工作区刚被改写，detect_changes() 的缓存立即失效。
    # 不清的话，后续任何 detect_changes()（无 refresh）都会返回**写入前**
    # 的旧快照，把刚落盘的改动当成「本地无改动」—— 这正是 pull 里那句
    # 「写入文件后再检测必须 refresh」注释所指，但要靠每个调用点自觉传参
    # 太脆弱，这里从源头失效掉。
    _RT.invalidate_changes()


def _git_blob_bytes(rev):
    """从本地 git 里取某个对象的原始字节（本地 HEAD 版本的 base 用得上）。"""
    # 大 blob 耗时长：按体积给一个宽松的动态超时，不能一律 120s。
    #
    # M6（A11 / A24）：上面这句注释**恰恰描述的是它没做的事** ——
    # 实现是 `timeout=DEFAULT_GIT_TIMEOUT`，正是注释禁止的"一律 120s"。
    # 而 `_timeout_for()`（每 MB 加 10 秒、30 秒保底、10 分钟封顶）
    # 早已存在、有 2 个调用点，就是没推广到这里。
    #
    # 先按对象体积算：cat-file -s 失败就退回兜底值。
    _size = 0
    _size_unknown = False
    try:
        _sp = subprocess.run(["git", "-C", ROOT, "cat-file", "-s", rev],
                             capture_output=True, timeout=30)
        if _sp.returncode == 0:
            _size = int((_sp.stdout or b"0").strip() or 0)
        else:
            _size_unknown = True
    except (OSError, subprocess.SubprocessError, ValueError):
        # N564：`except BaseException` 会连 KeyboardInterrupt / SystemExit
        # 一起吞掉。这里只需压住"探测失败"，用它的**真子集**即可 ——
        # 体积探测是告知性动作，Ctrl-C 必须能穿透。
        _size = 0
        _size_unknown = True
    if _size_unknown and not _size:
        # N563（第一百八十四轮，中危）：`cat-file -s` 失败时 `_size` 退回
        # 0，于是超时预算从"按体积"退化成保底值（实测 8MB：110s → 30s）
        # —— 而上面这段注释主张的恰恰是**不能一律固定**。
        # 退化本身可以接受（方向是更保守），但**静默**不可以。
        print(f"  ! 取不到 {rev} 的对象体积，超时预算退回保底 "
              f"{_timeout_for(0)}s（大文件可能因此超时）")
    try:
        proc = subprocess.run(["git", "-C", ROOT, "cat-file", "blob", rev],
                              capture_output=True, timeout=_timeout_for(_size))
    except subprocess.TimeoutExpired:
        # N563：整条链此前**零输出** —— 体积探测失败不吭声、预算退化不
        # 吭声、超时后 `return None` 也不吭声。而 `_base_bytes()` 拿到
        # None 后会说「无法确定」，或退化到 git 档（那个档的 docstring
        # 明写「可能已含用户自己的改动……不能用作合并基点」）。
        # 用户看到的是一句中性的"取不到"，看不到真实原因是超时。
        print(f"  ! 取 {rev} 的内容超时（>{_timeout_for(_size)}s，"
              f"体积约 {_size} 字节）—— 本次按取不到处理。")
        print("     可调大 PUSH_API_GIT_TIMEOUT，或改用 --api 通道。")
        return None
    return proc.stdout if proc.returncode == 0 else None


def _base_bytes(state, rel):
    """三方合并的 base = 「本地副本所基于的那个远端版本」。

    返回 **(data, source)**，source 说明这份 base 从哪来、有多可信：
      · "baseline"             —— 基线记录的远端版本。**唯一可信**的合并基点。
      · "baseline_unavailable" —— 基线**记了**，但基线记录的那份**历史
        版本**取不到（A6-J1）。注意与「远端当前内容拉取失败」是两回事：
        调用点在调用本函数**之前**已把远端当前版本取到了，所以走到
        这一档时**远端往往是好的**，取不到的是另一份（见 N525）。
      · "git"                  —— 基线没记，回退到本地 git HEAD 里的版本。
      · None                   —— 都没有（文件连 git 都没跟踪）。

    "baseline_unavailable" 与 "git" 必须分开，这是本函数最容易被抹平的一处：
    旧版在拉取失败时**继续往下走**到 git 分支，于是返回 (git版本, "git")。
    调用方据此打印「基线没记这个文件」—— 而基线其实记了，只是取不到；
    它建议的 --init-baseline 对网络抖动/限流完全无效，还会重写整份基线。
    更糟的是同一份 pull() 里，对**远端**内容拉取失败已做了精细区分
    （不进 conflicts、计入 failed），唯独 base 这一侧没有同等对待。

    为什么必须区分来源：git HEAD 里的版本**可能已经含用户自己的改动**。
    用户 `git commit` 之后，HEAD 就随之前进了；此时拿 HEAD 当 base，
    等于宣称「用户改动是共同祖先的一部分」，于是
    「远端没有你这行改动」会被误判成「远端故意删掉了你这行」——
    三方合并干净通过，用户的改动静默消失。实测踩到过。

    所以 "git" 这一档只能用来判断「远端有没有变过」，
    **不能**用作合并基点，也不能用来断言「本地没改动」。
    """
    entry = state["files"].get(rel)
    if isinstance(entry, dict) and entry.get("sha"):
        data = _remote_bytes(entry["sha"])
        if data is not None:
            return data, "baseline"
        # A6-J1：记了但取不到 ≠ 没记。绝不能继续往下走 git 那一档 ——
        # 那会把「可信来源拉取失败」伪装成「本来就没有可信来源」，
        # 并让调用方给出一条方向完全错误的诊断。
        return None, "baseline_unavailable"
    sha = git("rev-parse", f"HEAD:{rel}")
    if sha and len(sha) == 40:
        data = _git_blob_bytes(sha)
        if data is not None:
            return data, "git"
    return None, None                   # 无法确定 —— 调用方要有降级方案


def _apply_orphaned_task(state):
    """A6-J2：`--new-task` 时把上一个被接管的分支从 tasks 里摘掉。

    为什么必须与登记点分开：登记发生在建分支处，而那里到第一次
    save_state() 之间有 8 个提前退出点。原实现就在登记处 pop 并打印
    「已不再接管」，于是取消 / 无事可做 / --dry-run 时状态文件从未写回 ——
    磁盘上 task_branch 仍是旧分支，下次又回到它，而 --prune 已经把它
    当孤儿报出来了。

    放在**推送完成、save_state 之前**调用，声明才与落盘结果一致。
    """
    global _ORPHANED_TASK_OLD
    old = _ORPHANED_TASK_OLD
    _ORPHANED_TASK_OLD = None
    if not old:
        return
    (state.get("tasks") or {}).pop(old, None)
    print(f"  · 旧任务分支 {old} 已不再接管"
          f"（远端仍在，可用 --prune 查看、--delete-branch 清理）")


def _report_remote_deletions(deleted_remote):
    """打印「远端已删除、本地仍保留」的清单。

    抽成函数是因为它要在 pull() 的**两条**返回路径上打印：
      · 有文件需要合并时（循环之后）
      · 没有文件需要合并时（提前 return 之前）
    第二条恰恰是**最常见**的场景：远端只删了文件、别的都没动 →
    paths 为空 → 走到「没有需要合并的远端改动」直接 return。
    原先提示只写在循环之后，于是这个场景下完全静默 ——
    脚本不但不删，还连说都不说一声（实测：输出只有「没有需要合并的
    远端改动」）。
    """
    if not deleted_remote:
        return
    print(f"\n  ! 以下 {len(deleted_remote)} 个文件**远端已删除**，"
          f"本地仍保留（本脚本不会替你删本地文件）："
          f"{deleted_remote[:10]}")
    print("    它们仍在基线里：日后你一改，它们会作为「新增」被推回远端，"
          "相当于把别人删掉的文件复活。")
    print("    确认要删的话请手动删除本地文件，再跑 --pull。")


def _report_gone_both(gone):
    """打印「远端与本地都已删除、只剩基线记录」的清单。

    M74（A78，来自 fix_batch26，本轮预扫实测仍未修）：与
    `_report_remote_deletions` **同一个理由** —— 它也要在 pull() 的
    **两条**返回路径上打印。N353 修了分类（新增 `_gone_both`），但只
    把它接进了「没有需要合并的远端改动」那一条（paths 为空）。

    于是 paths **非空**时它算出来就丢掉（本轮实测：合并路径的收尾只有
    `_report_remote_deletions(deleted_remote)`，全程零行提到 gone_both）。
    而 paths 非空恰恰是**更常见**的场景 —— 远端既改了文件、也删了文件。

    丢掉的后果不是少一行字：`_gone_both` 是「基线里还有这条记录、但两边
    都没了」的唯一告知，用户据此才知道基线该清理；不说，它就一直躺在
    基线里，下次 `--status` 继续把它算进待删除候选。
    """
    if not gone:
        return
    print(f"\n  · 另有 {len(gone)} 个文件远端与本地都已删除，"
          f"仅剩基线记录待清理：{gone[:10]}")


# N623（第二百〇五轮，低危）：`force` 形参已删 —— 唯一调用点只传
# `only=` 与 `ignore_pending=`，函数体内零引用。留着它会诱导读者以为
# "--pull 侧有 force 生效"，而那是空的（N286 立的同一条原则）。
def pull(state, head_sha, rstate, only=None, ignore_pending=False):
    """把远端改动三方合并进本地，解决「检测到冲突却无法解决」的问题。

    没有这一步，第一层校验报「可能覆盖他人改动」后，本脚本给的出路只有
    --force-overwrite（因为 git 协议被网关拦死，用户没有 git pull 可用），
    于是唯一便捷出路就是摧毁防护本身。

    合并基（base）取**基线里记的远端 sha**——那正是本地副本所基于的版本，
    三方合并因此能分清「你改的」和「远端改的」。
    合并成功后基线更新为远端当前 sha：本地已包含远端全部内容，
    再推送时第一层校验 (远端 == 基线) 自然通过。
    """
    lmap = local_state_map()
    # 开始合并前的快照：之后每写一个文件，工作区状态就变了，
    # dirty 判断必须基于「合并开始前本地有没有自己的改动」。
    dirty0 = set(detect_changes(refresh=True))
    # A5-I1（第一百五十六轮复核，P1）：被拒的点名路径**必须记下来**。
    # 原先只 print 不记录，于是「点名的路径全部越界」→ paths 为空 →
    # 下面 `if not paths:` 那一支照常声明「已同步」并推进 synced_commit，
    # 而本次一个文件都没合并 —— P0-1（防止陈旧副本静默回退远端）被一次
    # 误操作解除。实测（`--pull ../etc/passwd`，远端 X 已变、本地仍旧）：
    # 打印「✅ 本地已包含远端全部改动」，synced_commit 被推进，X 一字未合。
    _rejected_only = []
    if only:
        paths = []
        for f in only:
            rel = safe_rel(f)
            if rel is None:
                print(f"  ! 忽略越界路径：{f}")
                _rejected_only.append(f)
            else:
                paths.append(rel)
    else:
        # 默认：远端变过 且 本地还没合上的
        #
        # 额外并入**本地根本没有**的文件。原判据要求「远端相对基线变过」，
        # 于是「远端新增 → init 把它的 sha 记进基线 → 之后远端没再变过」
        # 会被判成无需拉取，本地永远缺这些文件，--pull 还报告
        # 「没有需要合并的远端改动」并谎称已同步。实测 150 个新增文件
        # 一个都没拉下来（用户侧表现为「148 个文件缺失」）。
        # 本地没有内容可丢，补齐不存在覆盖风险，故无条件纳入。
        # 自动拉取的路径同样要过 safe_rel。
        #
        # 显式点名（--pull foo.txt）早就校验了，而这条**自动**路径没有：
        # 路径直接来自远端树，_write_local 会 makedirs + open 落盘。
        # 正常 GitHub 不会返回异常路径，但这与「远端内容不可全信」是同一个
        # 原则 —— 修 symlink 目标越界（P1-21）时已经认可了这一点，
        # 这里没理由留一个口子。纵深防御不该只在用户输入的那一侧做。
        # 判据必须比 (mode, sha) 元组，不能只比 sha。
        #
        # 只比 sha 时，「远端把文件从可执行改成不可执行（100755→100644）
        # 而内容不变」会被判成「远端没变过」→ 本地权限位永远拉不下来
        # → 每次推送又把它推回 755 → 该文件永久卡在待推列表里。
        # 与 T-11（只改可执行位推不上去）是同源的对称问题：
        # 那次修的是推送侧，这次是拉取侧。
        cand = sorted(p for p in rstate
                      if lmap.get(p) != rstate[p]
                      and (p not in lmap
                           or ((state["files"].get(p) or {}).get("mode"),
                               (state["files"].get(p) or {}).get("sha"))
                           != rstate[p]))
        paths = []
        for p in cand:
            rel = safe_rel(p)
            if rel is None:
                print(f"  ! 忽略越界路径（来自远端，已拒绝落盘）：{p}")
            else:
                paths.append(rel)

    # -------- 远端删除检测（C2）--------
    # 必须**比对基线**，不能只在遍历 rstate 时判断。
    #
    # rstate 是「远端现在有什么」。被删掉的文件根本不在里面，
    # 于是「远端已删除」这件事在循环里永远遇不到 —— 只在循环内加提示
    # 等于写了个永远不会执行的分支（实测：删掉的文件连一行输出都没有）。
    # 正确做法是拿基线（上次同步时远端有什么）与现在对比，差集即删除。
    # N52：原判据只看 base 与 rstate，不参考本地状态。
    # 于是用户照提示「手动删除本地文件」之后，本地已经没有了，
    # 而这句「本地仍保留」还在打印 —— 提示变假，且因为 --pull 从不清理
    # 基线里已不在 rstate 的条目，它会**永久重复**。
    #
    # 拆成两组分别说：本地还在的（要提醒可能复活）、本地已删的（只需清理基线）。
    _lmap_here = local_state_map()
    # N353（第一百一十轮，中危）：`_lmap_here` 是**索引**权威，
    # `rm a.txt` 但没 `git rm` 时索引里仍在 —— 于是被判成"本地仍保留"，
    # 而工作区里那个文件已经没有了。用户照提示 `rm a.txt` 会报
    # `No such file or directory`，且这句提示**永久重复**。
    #
    # 与 N284 / N306 是同一个根因的第三处：
    #   --status 待删除清单（N284）  已修（A∪B 并集）
    #   出口登记 / 出口重算（N306）  已修（pending_deletes_union）
    #   pull 的远端已删提示（本条）  未修，仍只用 lmap
    #
    # 所以这里补一条**工作区实际存在**的判据（等价于并集里的 B 源：
    # git status 能看见未 commit 的删除）。
    def _exists_here(_p):
        # M73（A78）：`os.path.exists` 对**悬空软链返回 False** ——
        # 链接本身还在，只是目标没了，却被判成"本地已删"。
        #
        # 于是悬空软链被分进 `_gone_both`（"远端和本地都删了"），
        # 而真正该进的是 `deleted_remote`（"远端删了、本地仍保留"）——
        # 后者附带的提示恰恰是这条分支存在的理由：
        #   「它们仍在基线里：日后你一改，它们会作为「新增」被推回远端，
        #     相当于把别人删掉的文件复活。」
        # 分错了类，这句警告就被跳过 —— 正是它要防的事故。
        #
        # 本处不删远端（方向保守），但**分类错了提示就没了**。
        #
        # 与 M17/M19 同一个根因：`_local_exists()` 是为这件事成立的
        # 唯一口径，用的正是 `lexists`。这里另写一份 `exists`
        # 就成了这条判据的**第四份实现** —— 而 `_local_exists` 的
        # docstring 里白纸黑字写着"必须收敛成一个函数"，
        # 它自己却被漏在了收敛范围之外。
        #
        # 直接复用。`_local_exists` 失败时向"不存在"倒（N364 已论证），
        # 与本处"判不出来就按还在"方向不同 —— 但那处的论证是
        # 「删除不可逆、判不出来就不删」，适用于删除判据；
        # 本处只是**分类展示**，不下删除决策，故沿用统一口径即可。
        return _local_exists(_p)

    deleted_remote = [p for p in sorted(state.get("files") or {})
                      if p not in rstate and p in _lmap_here
                      and _exists_here(p)]
    _gone_both = [p for p in sorted(state.get("files") or {})
                  if p not in rstate
                  and (p not in _lmap_here or not _exists_here(p))]

    if not paths:
        # 也必须刷新 synced_commit，否则用户会被 P0-1 **永久挡住**：
        #
        # 「待合并集合」是「远端相对基线变了 且 本地还没合上」。下列情况它为空，
        # 但主干确实前进过，P0-1 因此一直拦 —— 而提示给的出路只有 --pull
        # （正是这条空转的路径）和 --force-overwrite（摧毁防护）：死锁。
        #   ① 远端删了某个文件（脚本不支持删除，该文件不在 rstate 里）
        #   ② 远端改动后内容又等价（改回原样 / 只动了别处）
        #   ③ 用户已用别的途径把内容同步好了
        # 这些情况下「本地与远端内容一致」为真，声明已同步是真实的。
        print("\n没有需要合并的远端改动。")
        # 必须在这里也打印：paths 为空正是「远端只删了文件」的典型表现，
        # 若只在循环之后的正常路径打印，这个场景会完全静默。
        _report_remote_deletions(deleted_remote)
        # N52：本地也删了的那些，提示语不同（不能再说"本地仍保留"），
        # 否则用户照做之后提示变假并永久重复。
        # M74：改调函数 —— 下面那条（paths 非空）返回路径也要打同一句，
        # 两处各写一份就会像 N353 那样只改一处。
        _report_gone_both(_gone_both)
        # P0：必须在写 synced_commit / local_head 之前拦。
        # 这一支从头到尾只比了「远端 vs 基线」，**一次都没比过本地内容**；
        # 它打印的「已一致」是「远端与基线一致」，不是「你的文件与远端一致」。
        if _refuse_if_rewound(state, "--pull",
                              "   恢复后重跑 --pull，届时它会真正合并远端改动。"):
            return False
        # A5-I1：点名的路径**全部**被拒 → 本次零合并，不得声明已同步。
        #
        # 上面那句「没有需要合并的远端改动」是**只就点名范围**而言 ——
        # 点名范围此刻是空的，所以恒真，与远端有没有改动无关。照它声明
        # 「已同步」，等于用一个空集合作出的结论去解除 P0-1。
        #
        # 与 N243 同源（收尾处 `if only:` 不写 synced_commit）：那处管的是
        # 「点名了、也合并了」，本处管的是「点名了、一个都没剩下」——
        # 两条出口，缺任一都留一个洞。
        if only and not paths:
            print(f"\n  ! 你点名的 {len(_rejected_only)} 个路径全部无效"
                  f"（越界或非法），本次**没有合并任何文件**。")
            print(f"    被忽略：{_rejected_only[:5]}"
                  f"{' …' if len(_rejected_only) > 5 else ''}")
            print("    上面那句「没有需要合并」只就点名范围而言（该范围已空），"
                  "不代表远端没有改动。")
            print("    **不**声明已同步（否则会解除 P0-1 闸门）；"
                  "检查路径后重跑 --pull。")
            return False
        if state.get("conflicts"):
            print("  ! 但有未解决的冲突，暂不标记为已同步。")
        else:
            # N460（第一百五十一轮，中危）：这一支既然要声明「已同步」，
            # 就该同时把「本地 == 远端」的基线条目跟上去。
            #
            # 「待合并集合」为空有两种成因，本支不区分：
            #   (a) 远端 == 基线           → 确实无变化，输出成立
            #   (b) 远端 ≠ 基线、本地 == 远端 → **远端变了，只是本地已合上**
            #
            # (b) 下「远端相对基线无变化」是**假陈述**，且紧跟着的
            # 「标记为已同步，可正常推送」也不成立 —— 基线仍是旧值，
            # 而基线是「远端文件是否被他人改动」的唯一参照。
            #
            # 与 N459 同源：L5975 那段注释已处理过这一类假话的**另一种
            # 成因**（「本地缺失」），本处是「本地已合上、基线旧」——
            # 两类成因共享同一句输出，只修了前者。
            #
            # 判据 lmap == rstate，与 mark_synced / N459 完全一致，
            # 不另起一份。
            _n460 = []
            for _p, (_rm, _rs) in (rstate or {}).items():
                if _lmap_here.get(_p) != (_rm, _rs):
                    continue
                _cur = state["files"].get(_p)
                if not _baseline_stale(_cur, _rm, _rs):
                    continue
                state["files"][_p] = {"mode": _rm, "sha": _rs}
                _n460.append(_p)
            state["base_commit"] = head_sha
            state["synced_commit"] = head_sha
            state["local_head"] = _local_head()
            save_state(state)
            if _n460:
                print(f"  · {len(_n460)} 个文件本地已与远端一致，"
                      f"基线已跟进（此前记为旧版本）。")
            # P2-1：旧文案「本地内容已与远端一致」**是假的** ——
            # 这一支只比了「远端 vs 基线」，从未比过本地内容。
            # 措辞越肯定，出错时越误导：用户据此相信本地已经和远端一样了，
            # 于是放心推送，本地旧版本静默覆盖远端（P0 链路第 4 步）。
            print(f"  · 远端相对基线无变化，标记为已同步（{str(head_sha)[:8]}），"
                  f"可正常推送。")
        return True

    print(f"\n合并 {len(paths)} 个文件的远端改动（base = 基线记录的远端版本）：")
    clean, conflicted, failed, moved_aside = [], [], [], []
    # N525：failed 记「远端**当前**内容没取到」；failed_base 记「基线记录的
    # 那份**历史**版本没取到」。两者处置相同（都不进 conflicts、都重跑即可），
    # 但归因不同 —— 混在一个列表里，收尾就只能给一句对其中一半为假的文案。
    failed_base = []
    # 预先算出「已搬走的旧路径」→ 新路径。只在本地**确实存在**对应新文件时
    # 才算数：新文件也被删了的话，内容就没了着落，必须照常拉回来。
    # 优先用**记录的**对应关系（见推送时的 detect_moves 注释）：
    # 事后推断配不出对 —— 推完那次新路径就进了基线，「新增」集合为空。
    # 记录缺失时才退回现场推断，兜住「从没推过就已搬家」的情况。
    _moved_away = {}
    try:
        _lm = local_state_map()
        _ma_in = dict(state.get("moved_away") or {})
        _moved_away = {o: n for o, n in _ma_in.items()
                       if n in _lm and o not in _lm
                       # N373：远端旧路径上已换成别的内容 → 记录失效
                       and _moved_still_valid(rstate, o, _lm, n,
                                            state.get("files"))}
        if not _moved_away:
            for _o, _n in detect_moves(state, _lm)[0]:
                if _n in _lm and _moved_still_valid(rstate, _o, _lm, _n,
                            state.get("files")):
                    _moved_away[_o] = _n
        # N594（第一百九十三轮，低危）：把**失效**记录就地摘掉。
        #
        # 这一段此前只**重算**给本次循环用，从不写回 state。于是只拉不推
        # 的用户那份 state 只增不减 —— 实测连跑 3 次 --pull，失效记录
        # 依旧原样躺在 state["moved_away"] 里。pull 的**行为**每次都对
        # （重算嘛），代价是状态文件缓慢膨胀，且 --scope 侧每次都要重新
        # 判一遍（N474 描述的「持续输出错误 tag」，只是入口不同）。
        #
        # 与推送侧**同源**：直接调 `_prune_moved_away`（它也是
        # `_moved_still_valid` 判失效），不在这里另写一份过滤条件 ——
        # 分开写两遍必然继续漂移，而漂移的表现正是本批反复记的
        # 「同一份 state、两个出口给出相反结论」。
        #
        # 只摘不增：上面 detect_moves 兜底推断出来的新记录**不**落盘，
        # 那是现场推断，写进 state 会让一次误判长期固化。
        _kept = _prune_moved_away(_ma_in, _lm, rstate, state.get("files"))
        if len(_kept) != len(_ma_in):
            if _kept:
                state["moved_away"] = _kept
            else:
                state.pop("moved_away", None)
    except Exception as e:
        print(f"  ! 搬家检测失败（按常规处理）：{type(e).__name__}: {e}")
    for _idx, rel in enumerate(paths):
        # M91（A96）：额度已尽就别再逐个重试了。
        # 剩余文件计入 failed —— 复用现有机制：末尾"拉取失败 N 个"
        # 自动正确，`conflicted or failed` → 返回 False → 退出码 1
        #（运行错误，符合契约）。不是冲突，不进 conflicts。
        if _RATE_LIMITED and _idx > 0:
            print(f"\n  ! 限流额度已用尽：剩余 {len(paths) - _idx} 个文件"
                  f"本次不再请求（它们已计入「拉取失败」，"
                  f"等额度重置后重跑 --pull 即可）。")
            failed.extend(paths[_idx:])
            break
        rmode, rsha = rstate.get(rel, (None, None))
        if rsha is None:
            # 不落地删除，但必须**登记并集中提示**。
            #
            # 远端删掉的文件会一直留在 state["files"] 基线里，本地文件也
            # 一直保留。日后本地一改这个文件，它就会被当作「普通改动」
            # 重新推上去 —— 把别人删掉的东西复活了，且无人知晓。
            # 脚本不支持删除（删除是不可逆的远端操作，不该由同步工具代劳），
            # 但至少要让用户知道「这些文件远端已经没有了」。
            # N75：这里把「rstate 里查不到」一律当成「远端已删除」。
            # 自动路径不会触发；但**点名路径**（--pull <文件>）只做
            # safe_rel 校验、不校验远端是否存在 → 用户点了一个远端从来
            # 没有过的路径，会被报成"远端已删除"。
            # 那条提示接着说"确认要删的话请手动删除本地文件"——
            # 用户照做就把一个根本没问题的文件删了。
            if rel in (state.get("files") or {}):
                deleted_remote.append(rel)
            else:
                print(f"  ! {rel} 远端不存在、也不在基线里，跳过。"
                      f"点名 --pull 时请确认路径拼写。")
            continue
        remote = _remote_bytes(rsha)
        if remote is None:
            # 拉取失败**不是冲突**。
            #
            # 冲突 = 「拿到了远端内容，但合不上」，是**做过了**；
            # 拉取失败 = 「没拿到」，是**没做成**。混进 conflicts 有三个后果：
            #   · state["conflicts"] 被持久化 → 后续所有推送被全局拦截
            #   · 它没有 .remote / .base 副本，用户无从下手
            #   · 解除方式只有 --resolve，而 resolve 只是清标记 —— 等于逼用户
            #     对一次网络抖动做一次毫无意义的「冲突解决」
            # 一旦 conflicts 里混进非冲突项，这个列表就失去了语义。
            failed.append(rel)
            continue
        local = _local_bytes(rel)
        base, base_src = _base_bytes(state, rel)

        if base_src == "baseline_unavailable":
            # A6-J1：与上面「远端内容拉取失败」同一处置 —— 计入 failed、
            # 跳过，**不进 conflicts**。理由完全一致：
            #   · 混进 conflicts 会写进 state["conflicts"] → 后续推送被全局拦截
            #   · 它没有 .remote / .base 副本，用户无从下手
            #   · 解除方式只有 --resolve，等于逼用户对一次网络抖动做
            #     一次毫无意义的「冲突解决」
            # 也不能沿用旧版的静默降级：那会用不可信的 git HEAD 当合并基点，
            # 得到「假冲突 + 错误诊断 + 无效解法」三件套。
            #
            # N525（第一百七十三轮，中危）：上面三处文案（本句、docstring、
            # pull 收尾）一律写「远端内容拉取失败」，而**失败的是基线记录的
            # 那份历史版本** —— 远端当前内容在上一行 `_remote_bytes(rsha)`
            # 已经取到了（实测 s2 返回成功、s1 返回 None）。
            #
            # 用户读到「远端内容拉取失败」会以为这一轮没拿到远端，实际拿到了。
            # 这与 A6-J1 自己的立论直接冲突：它的 docstring 逐字写着旧版的
            # 害处是「调用方据此打印『基线没记这个文件』—— 而基线其实记了，
            # 只是取不到」。修完后枚举名是准确的，人类文案却退回成"远端内容"
            # —— 换了个错法，仍然是错误归因。
            #
            # 分开记账：failed_base 与 failed（远端当前）分开，收尾分别打印。
            print(f"  ! {rel}：基线记了这个文件，但**基线记录的那份历史版本**"
                  f"取不到（远端当前内容已取到），本次跳过")
            print("    不是冲突，改动也没丢 —— 重跑 --pull 即可，"
                  "不需要 --resolve、也不需要 --init-baseline。")
            failed_base.append(rel)
            continue

        if local is None:
            # 本地根本没有这个文件：远端新增，或本地副本不完整。
            # 必须**先于**下面的「远端相对基线没变」判断处理 ——
            # 否则 base == remote（基线记的就是远端当前 sha）会让它
            # 走进「远端无变化，本地改动原样保留」而跳过，永远补不上。
            # 本地没有内容可丢，直接落盘不存在覆盖风险。
            #
            # 例外：**已搬走的旧路径不要拉回来**。
            # 实测：搬走 A→B 后跑 --pull，A 会被重新落盘，本地出现两份；
            # 下次推送又可能把 A 推上去 —— 搬家与 pull 互相打架，
            # 这正是「问题反复出现」的一部分。
            #
            # 判据要求「旧路径的基线 sha == 某个新增路径的本地 sha」
            # （见 detect_moves），即内容完全相同才认作搬家。
            # 严格是必须的：认错了会让该拉的文件拉不到，是数据缺失，
            # 比多拉一个旧副本严重。
            if rel in _moved_away:
                # N398：跳过后原本 append 到 clean —— 于是收尾照旧打印
                # "✅ 本地已包含远端 X 的全部改动"，而该文件本地**确实没有**
                # （只有内容相同的新路径）。按 --scope 的 hint 跑 --pull
                # 会永远拉不回，且每次都报成功。
                #
                # 不改成报错（搬家判定本身是对的，旧路径不该拉回），
                # 改成**不计入 clean**：让"已包含全部改动"这句话不再
                # 覆盖它，并在收尾显式列出。
                print(f"  · {rel} 已搬到 {_moved_away[rel]}，跳过"
                      f"（不拉回旧路径；本地缺失是搬家的预期结果）")
                moved_aside.append(rel)
                continue
            _write_local(rel, remote, rmode)
            state["files"][rel] = {"mode": rmode, "sha": rsha}
            clean.append(rel)
            print(f"  ↓ {rel} 本地缺失，已从远端补齐")
            continue

        if local == remote:
            # 内容相同 ≠ 状态一致：还要同步 mode。
            #
            # 这里只比较了内容字节（_local_bytes），不比较权限位。
            # 远端 100755→100644 而内容不变时会走进这一支，旧版只把基线
            # 记成 100644 却**没有 chmod 本地文件**：
            #   · 基线说 100644，本地实际 0755
            #   · local_state_map() 算出 100755 ≠ 基线
            #   · 下次再判定有差异 → 又走进这里 → 又只改基线不 chmod
            # 于是本地权限位与基线永久打架，该文件每次都被报成待推。
            # 与 _write_local 里「非执行文件显式回退权限位」是同一个道理，
            # 只是那处在写文件时顺手做了，这一处漏了。
            cur_mode = (lmap.get(rel) or ("100644", ""))[0]
            if cur_mode != rmode:
                _write_local(rel, remote, rmode)
                print(f"  = {rel} 内容一致，仅权限位 {cur_mode} → {rmode}")
            state["files"][rel] = {"mode": rmode, "sha": rsha}
            if cur_mode == rmode:
                print(f"  = {rel} 已一致")
            clean.append(rel)
            continue
        if base is not None and base == remote:
            # 远端相对基线没变 → 这个文件远端根本没动，本地保留自己的改动即可。
            # 必须计入 clean：若所有文件都走这条分支，clean 会为空，
            # synced_commit 就不刷新，用户随即被 P0-1 永久挡住（同 P1-2）。
            #
            # 【必须同时刷新基线】base == remote 已证明「本地副本所基于的版本
            # 与远端当前内容逐字节相同」，此刻记下 rsha 是准确的。
            # 不记的话基线缺口永远补不上，两个后果同时发生：
            #   · direct 模式：推送时 base 仍缺失 → 被第一层判 blocked
            #     → 建议 --pull → --pull 又走回这一支 → 再推再拦，无限循环。
            #     用户严格按提示操作也走不通（实测死锁）。
            #   · PR 模式：_local_matches_baseline() 基线缺失时保守返回 False，
            #     调用方一律读成「本地确实改过」→ 归入 overlap 无条件放行
            #     → 本地旧内容整文件覆盖主干，静默回退（比死锁更糟：没人知道）。
            # 两个 P0 同一个根因，补这一处即同时消失。
            #
            # 此处 base 必可信：走到这一支说明 local != remote（相等的话
            # 上面的 `local == remote` 分支已拦截），而 base == remote，
            # 故 local != base —— 正是不再信任 git HEAD 那份 base 的条件。
            # M167（A152，低危）：**动作**与**文案**自相矛盾。
            #
            # 这一支的前提是「base == remote」（内容逐字节相同），所以旧文案
            # 无条件打印「远端无变化」。但 N630（A128）之后判据含 mode ——
            # 远端只把某个文件从 100644 改成 100755、内容一字未变时，
            # `_baseline_stale` 判 True，**这里真的会改写基线**。
            #
            # 于是同一支里：动作承认远端变了（才刷的），文案说远端没变。
            # 用户排查「为什么我本地权限位和远端不一致」时，看到的正是
            # 这一句「远端无变化」，会把真正的差异来源排除掉。
            #
            # 与 A150 的 M163 同族：改了动作没改说法。修法是文案随动作分支，
            # 而不是二选一 —— 「无变化」这一档在真正无变化时仍然要说。
            if _baseline_stale(state["files"].get(rel), rmode, rsha):
                state["files"][rel] = {"mode": rmode, "sha": rsha}
                print(f"  · {rel} 远端内容无变化、权限位有更新"
                      f"（基线已跟进），本地改动原样保留")
            else:
                print(f"  · {rel} 远端无变化，本地改动原样保留")
            clean.append(rel)
            continue

        # git HEAD 那份 base 可信吗？取决于用户有没有把改动 commit 进去：
        #   · 工作区已比 HEAD 新（local != base）→ 改动还没进 HEAD，
        #     HEAD 仍是纯净的远端版本，可作共同祖先 —— **可信**
        #   · 工作区与 HEAD 一致（local == base）→ 无法排除「用户已 commit」，
        #     而 commit 后 HEAD 就含其改动了 —— **不可信**
        if base_src == "baseline":
            base_ok = True
        elif base_src == "git":
            base_ok = (local != base)
            #
            # 判定为**可信**的这一支同样要说清风险，不能静默过。
            #
            # `local != base` 只证明「工作区比 HEAD 新」，推不出「HEAD 是纯净的
            # 远端版本」——用户完全可能是：先 commit 了改动 A，又在工作区加了
            # 改动 B。此时 HEAD 里已经含 A，拿它当 base 等于宣称「A 是共同祖先
            # 的一部分」，于是「远端没有 A」被判成「远端故意删掉了 A」。
            #
            # 实测：合并零冲突、干净通过，而 A 静默消失 —— 丢的是**用户自己的
            # 已提交改动**，远端那一份反而完整保留。不崩、不拦、无冲突标记。
            if base_ok:
                print(f"  ! {rel}：基线没记这个文件，正用本地 git HEAD 的版本当合并基点。")
                print("    这只在「你的改动还没 commit 进 HEAD」时才成立；")
                print("    若你已 commit 过改动，那部分会**被当成共同祖先**而悄悄丢失")
                print("    （零冲突、无提示）。核对结果里有没有少东西。")
                print("    补上基线可消除此风险：python3 push_api.py --init-baseline")
        else:
            base_ok = False

        # 「本地有没有自己的改动」必须用 local == base 判定，不能用
        # 「工作区是否有未提交改动」：推送成功后脚本会在本地补一个 commit，
        # 工作区随之变干净，按后者判断就成了「本地无改动」→ 直接用远端覆盖，
        # 把你刚推上去的改动从工作区抹掉（还在本地 git 历史里，但文件没了）。
        #
        # 但 `local == base` 只在 base **可信**时成立。若 base 是不可信的
        # git HEAD（用户已 commit），则 local == HEAD 恒成立 ——
        # 判成「本地无改动」→ 用远端覆盖，用户已 commit 的改动被静默抹掉
        # （实测：改动消失且零冲突提示）。这种情况按**有改动**处理。
        if base is not None and base_ok:
            no_local_change = (local == base)
        elif base is not None:
            no_local_change = False         # base 不可信，不敢断言
        else:
            # M160（A146，高危）：原先这里是
            #     no_local_change = rel not in dirty0   # 没 base，退回工作区脏检查
            # 而「base 取不到」恰恰会在**用户已 commit 改动**时命中：
            # 远端 v1、本地改成 v2 并 commit → 工作区**干净** → 判成
            # 「本地无改动」→ 下面直接 `_write_local(remote)`，v2 被 v1 覆盖，
            # 且收尾报「✅ 本地已包含远端全部改动，可直接推送」。
            # 用户照这句推送 → 远端也被回退：本地工作区与远端**两处同时丢**。
            #
            # 完整链条（A146 实测）：大 blob → `cat-file -s` 失败（N563 静默
            # 归零）→ 超时预算退化 → `cat-file blob` 超时 → `_git_blob_bytes`
            # 返回 None → `_base_bytes` 返回 (None, None) → 本分支命中。
            # N563 让超时**可达**，本条让超时**有严重后果**。
            #
            # 与 `base is not None and not base_ok`（base 不可信）同等处置：
            # 两者都是「基点未知」，都按**有改动**处理 —— 于是往下走三方合并，
            # `merge_base = base if base_ok else b""`（见下）退化成空 base，
            # 两侧都当新增 → 宁可假冲突，也不静默丢改动。
            #
            # 代价：远端单方面前进、本地确实没动过的场景（base 缺失时）
            # 会拿到一个假冲突而不是自动更新。方向是 fail-closed ——
            # 假冲突有副本可比对、可 --resolve；静默覆盖无法撤销。
            no_local_change = False
            print(f"  ! {rel}：合并基点取不到（基线没记，git HEAD 那份也读不到"
                  f"—— 常见成因是大文件读取超时），不敢断言本地没有改动。")
            print("    已按**有改动**处理：不会用远端内容覆盖你的本地版本，"
                  "而是走三方合并（可能报冲突，改动不会丢）。")
            print("    确认本地确实没有自己的改动后，"
                  "可跑 --init-baseline 补上基点再 --pull。")

        if no_local_change:
            # 本地没有自己的改动，只是版本旧：直接更新到远端版本即可，
            # 不存在要保留的内容，硬走三方合并只会制造假冲突。
            _write_local(rel, remote, rmode)
            state["files"][rel] = {"mode": rmode, "sha": rsha}
            clean.append(rel)
            print(f"  ↓ {rel} 本地无改动，已更新到远端版本")
            continue

        # 用独立的 ignore_pending，不再复用 force。
        #
        # force 在推送路径上的语义是「覆盖远端」（放行文件级校验）；
        # 而这里的语义是「跳过自己上次未解决的冲突」。两者毫无关系，
        # 复用会让用户以为自己在「覆盖远端」，实际是「关掉冲突保护」。
        # 与 A-15「放行粒度与承诺不一致」是同一族问题。
        if not ignore_pending and rel in (state.get("conflicts") or []):
            print(f"  ! {rel} 上次冲突尚未解决（先手工改好再 --resolve {rel}），跳过")
            conflicted.append(rel)
            continue

        # A3（P2）：这段曾被两份审查报告判为死代码，早先的注释声称
        # 「修『远端新增文件拉不下来』时调整了控制流，它才重新可达」——
        # **这句话是错的**。AST 实证（两处 `local is None`，上一处 body
        # 以 continue 结束，且两处之间 local 未被重新赋值）：
        #     (6574, Continue) (6723, Continue)
        # 本处**至今仍不可达**。
        #
        # 留着它是有意的：一旦上面那处的控制流被改（例如改成不 continue、
        # 或在这之间给 local 重新赋值），这里立刻重新成为「远端新增文件
        # 拉不下来」的唯一补救路径。删掉它等于把那个 bug 重新挖好。
        # 真正有害的是**声称它可达的那句注释** —— 下一个人读到会以为
        # "作者验证过"从而不再核查，比没有注释更糟（与 A1 的 docstring
        # 承诺"只报告不删"、实际会删是同一类：文档覆盖了事实）。
        if local is None:                   # 本地没有：远端新增，直接落盘
            _write_local(rel, remote, rmode)
            state["files"][rel] = {"mode": rmode, "sha": rsha}
            clean.append(rel)
            print(f"  ↓ {rel} 远端新增，已写入本地")
            continue

        if rmode == "120000" or lmap.get(rel, (None,))[0] == "120000":
            # symlink 内容是路径字符串，行合并没有意义 —— 但**不能直接采用
            # 远端**。
            #
            # 走到这里意味着 no_local_change 判定已经通过（见上方），
            # 即本地确实有改动。旧版却直接 _write_local 覆盖并计入 clean：
            #   · 本地改的指向被静默丢弃，无备份
            #   · 报「干净 N 个，冲突 0 个」—— 把丢弃说成顺利
            # 同函数内普通文件冲突是写 .remote 副本 + 计 conflicted，
            # symlink 这里也必须一样，否则同一类改动两种处置。
            if local != remote:
                # L1（A8）修的是「_conflict_path 建不出目录时别把整个 pull
                # 带崩」，返回值 None 由它给出；而这里原本**无视返回值**，
                # 照旧打印「远端目标已存到 {lp}」——实测输出是
                # "远端目标已存到 None"。
                # 用户拿它去比对，而磁盘上根本没有这个文件；同一份 pull()
                # 里远端拉取失败有 failed 列表兜底，辅助产物写失败却会
                # **谎报成功**。所以这里用返回值，写不出来就把远端目标
                # 直接打出来（symlink 目标是短字符串，可直接看）。
                lp = None
                # N631（接上）：symlink 这一支同样塌缩 —— 见上面普通文件
                # 那一处的说明。这里也带出成因，让措辞不再写死成一种。
                _why_lp = ""
                try:
                    lp = _write_conflict_artifact(rel, "remote", remote)
                except OSError as e:
                    print(f"  ! 写 {rel}.remote 失败：{e}")
                    _why_lp = f"写入失败：{e}"
                else:
                    if not lp:
                        _why_lp = "副产物目录不可用"
                conflicted.append(rel)
                print(f"  ✗ {rel} 是符号链接且本地改过指向，无法自动合并")
                if lp:
                    print(f"     远端目标已存到 {lp}，手工选一个后 --resolve {rel}")
                else:
                    print(f"     ⚠ 冲突副本未能写出"
                          f"（{_why_lp or '副产物目录不可用'}），"
                          f"远端目标是："
                          f"{remote.decode('utf-8', 'replace')!r}")
                    print(f"     手工选定指向后运行："
                          f"python3 push_api.py --resolve {rel}")
                continue
            _write_local(rel, remote, rmode)
            state["files"][rel] = {"mode": rmode, "sha": rsha}
            clean.append(rel)
            print(f"  ! {rel} 是符号链接，不合并，已直接采用远端目标")
            continue

        # 合并基点**只认来自基线记录的那一份**。
        #
        # base 为 None（基线没记 + git HEAD 里也没有，例如 untracked 新文件）
        # 或 base 来自 git HEAD 时，都退化成「空 base」跑三方合并 ——
        # 两侧都被视为新增，容易判成冲突，但宁可假冲突也不能静默丢改动：
        # git HEAD 那份在用户 commit 之后已含其改动，拿它当基点会让
        # 「远端没有你这行」被误读成「远端故意删了你这行」，合并干净通过
        # 而改动消失。后续有 --resolve 兜底。
        # N520（第一百七十一轮，中危）：`--pull --ignore-pending` 是**唯一**
        # 能补出冲突副本的路径（见 N518），但它有个副作用：若用户此时已经
        # 手工合并好（文件里没有冲突标记了），正常三方合并会把他的合并结果
        # **重新包回冲突标记** —— 实测：
        #   ① 目录不可用 → pull：a.txt = 带标记的冲突内容
        #   ② 用户手工改成 'RESOLVED-BY-HAND\n'
        #   ③ --pull --ignore-pending：a.txt 又被包回标记，
        #      内容在 `<<<<<<< 本地` 段里（没丢），但 --resolve 会再次以
        #      "仍有冲突标记"拒绝 → 手工合并必须重做。
        #
        # 而用户跑这条命令的目的恰恰只是"补个副本"。所以：已无冲突标记
        # = 用户已处理过 → **不重新合并**，只补写副本（副本已存在则不动）。
        if (ignore_pending and rel in (state.get("conflicts") or [])
                and local is not None and not _has_conflict_markers(local)):
            _need = []
            for _k, _v in (("remote", remote), ("base", base)):
                _p = _conflict_path(rel, _k)
                if _v is None:
                    continue
                if _p is None or not os.path.exists(_p):
                    _need.append(_k)
            if not _need:
                print(f"  ✓ {rel} 你已手工合并过（无冲突标记），副本齐全，"
                      f"本次不动这个文件")
            else:
                _ok = []
                for _k in _need:
                    _v = remote if _k == "remote" else base
                    try:
                        if _write_conflict_artifact(rel, _k, _v):
                            _ok.append(_k)
                    except OSError as _e:
                        print(f"  ! 写 {rel}.{_k} 失败：{_e}")
                if _ok:
                    print(f"  ✓ {rel} 你已手工合并过（无冲突标记），"
                          f"本次只补写副本：{' / '.join(_ok)}"
                          f"（未重新合并，你的合并结果保留）")
                else:
                    print(f"  ! {rel} 你已手工合并过（无冲突标记），"
                          f"但副本仍写不出（副产物目录不可用），未重新合并")
            # 仍在 conflicts 里：保持现状，等用户 --resolve 时清掉。
            conflicted.append(rel)
            continue
        merge_base = base if base_ok else b""
        merged, bad = _merge3(local, merge_base, remote)
        if bad or merged is None:
            conflicted.append(rel)
            # .base 只在**确实知道** base 时才写。base 是 None 时
            # `f.write(base)` 会抛 TypeError（实测），整个 pull 崩在半路；
            # 也不能写成空文件 —— 那等于宣称「合并基点是空文件」，
            # 与「不知道基点」是两回事，会误导人工比对。
            # 先清掉上一轮冲突的残留。副产物目录在仓库外、跨运行保留，
            # 若这轮基点未知而上轮写过 .base，陈旧文件会让人以为
            # "有共同祖先可比对" —— 比没有更误导。
            # N513（第一百六十八轮，中危）：清理与写入**共用** _conflict_path()。
            # 副产物目录不可用时它返回 None，`if p and …` 直接短路 ——
            # 新副本写不出（A8 已修，会提示），**旧副本也删不掉（本条，零提示）**。
            # 实测：① 目录可用、远端 v2 → .remote = remote-v2；
            #       ② 目录被占成文件、远端前进到 v3 → 新的写不出、旧的删不掉；
            #       ③ 恢复目录 → 磁盘上出现上一轮的 remote-v2，
            #          而用户要对的是 remote-v3。
            # A8 那句「本次没有可比对的文件」就本次运行而言是真的，
            # 但目录一恢复，磁盘上立刻出现一个**可比对的文件**——而它是陈旧的。
            # 上面这段注释自己写过陈旧副本「比没有更误导」，专门为它写了清理；
            # 只是清理本身也会失效，而失效时没有任何输出。
            _stale_left = False
            for stale in ("remote", "base"):
                p = _conflict_path(rel, stale)
                if p is None:
                    _stale_left = True
                    continue
                if os.path.exists(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        _stale_left = True
            if _stale_left:
                print(f"  ! {rel} 的旧冲突副本**未能清理**（副产物目录不可用）——"
                      f"恢复目录后磁盘上可能残留**上一轮**的内容，"
                      f"请先手工核对时间戳/内容再拿它比对。")
            # L1（A8）只做到了「不崩」：`_conflict_path` 建不出目录时返回
            # None，而这里原本**无视返回值**，照旧打印「已生成 …供比对」。
            # 实测（父路径被占成文件）：副本一个都没写，输出却是
            # "已生成 a.txt.remote / a.txt.base 供比对"  ★
            # 同一份 pull() 里远端拉取失败有 failed 列表兜底 + 明确指引，
            # 辅助产物写失败却谎报成功 —— 与 A6-J1 是镜像的两条：
            # 那边「失败静默降级」，这边「失败谎报成功」。
            # N631（A129 的 M139，低危）：「写不出副本」在调用方其实有
            # **两种**成因，而 `_w_remote` / `_w_base` 只是两个布尔：
            #   ① `_conflict_path` 返回 None —— 目录建不出来（真·目录不可用）
            #   ② `os.open` / `write` 抛 OSError —— 目录是好的，写不进去
            #      （磁盘满 ENOSPC、只读挂载 EROFS、配额 EDQUOT …）
            # 两者塌缩成同一个 `False`，于是措辞与指引只能按最常被想到的
            # 那一种写死。A129 实测：目录**可用**时注入 ENOSPC，输出是
            #   ! 写 a.txt.remote 失败：[Errno 28] No space left on device
            #   ⚠ 冲突副本未能写出（副产物目录不可用）…   ★ 与前一行自相矛盾
            #   可恢复目录后重跑 --pull …                  ★ 目录此刻是好的
            # 同一段输出里前一行给出真因、后一行把它归成另一种，指引指向
            # 对该成因**无效**的动作。
            #
            # 与脚本反复立的「依据是错的和没有依据一样糟」同源：这里不是
            # 没有依据，是把依据换成了错的那个。所以带出**原因**而不是
            # 真假，下面按成因分流措辞与指引。
            _w_remote = False
            _why_remote = ""
            try:
                _w_remote = bool(
                    _write_conflict_artifact(rel, "remote", remote))
            except OSError as e:
                print(f"  ! 写 {rel}.remote 失败：{e}")
                _why_remote = f"写入失败：{e}"
            else:
                if not _w_remote:
                    _why_remote = "副产物目录不可用"
            _w_base = False
            _why_base = ""
            # 只有**可信**的 base 才落盘成 .base。git HEAD 那份在用户 commit
            # 之后已含其改动，写出去会让人误以为那是共同祖先。
            if base_ok:
                try:
                    _w_base = bool(_write_conflict_artifact(rel, "base", base))
                except OSError as e:
                    print(f"  ! 写 {rel}.base 失败：{e}")
                    _why_base = f"写入失败：{e}"
                else:
                    if not _w_base:
                        _why_base = "副产物目录不可用"
            if merged is not None:
                _write_local(rel, merged, rmode)
            if _w_remote:
                # M158（A144，低危）：这一句里的 `{rel}.remote` 是**仓库内相对
                # 路径**的写法，而磁盘上的名字经过编码（`sub/a.txt` 落盘是
                # `sub%2Fa.txt.remote`，见 _conflict_basename）。A95 只新增了
                # 下面那行真实绝对路径、**没改上面这句**，于是同一段输出并列
                # 两个名字，其中一个根本不存在 —— 嵌套目录的用户按第一句去找，
                # 找不到。改成两处都用磁盘上的真实文件名。
                _rp = _conflict_path(rel, "remote")
                _rn = os.path.basename(_rp) if _rp else f"{rel}.remote"
                if _w_base:
                    _bp = _conflict_path(rel, "base")
                    _bn = os.path.basename(_bp) if _bp else f"{rel}.base"
                    hint = f"{_rn} / {_bn} 供比对"
                elif base_ok:
                    hint = (f"{_rn} 供比对"
                            f"（{rel}.base 未能写出）")
                else:
                    hint = (f"{_rn} 供比对（合并基点不可信，"
                            f"未生成 {rel}.base）")
                print(f"  ✗ {rel} 冲突：已生成 {hint}")
                # A95 问题 3（告知侧）：上面那句**没说副本在哪**，而它
                # 落在**仓库外**（`_conflict_path` 的 docstring 列了三条
                # 理由，其中"未跟踪文件会被 detect_changes 拾取"是决定性的）。
                # 于是用户知道"有副本供比对"，却不知道去哪儿找 ——
                # 只能翻源码找 CONFLICT_DIR。
                #
                # 而且 `{rel}.remote` 这个写法本身也不准：磁盘上的名字经过
                # 编码（分隔符转义，见 _conflict_basename），`sub/a.txt` 落盘
                # 不是 `sub/a.txt.remote`。嵌套目录的冲突文件按这句去找，
                # 找到的是一个不存在的路径。
                #
                # 打印**真实绝对路径**，并把「整份用上游」这条一步到位的
                # 路也写出来 —— 三方合并对"整份重写"必然产出冲突，而用户
                # 真正想要的是二选一，不是逐行比对合并结果。
                if _rp:
                    print(f"     远端副本：{_rp}")
                    # M156（A144，中危）：这条命令的意图就是让人**原样复制粘贴
                    # 到 shell**，而两个操作数都是仓库里的路径、都没加引号：
                    #   · 路径含空格 → `cp: target 'b.txt' is not a directory`
                    #     （rc=1），用户看不出是空格问题
                    #   · 文件名含 `$( )` → 命令替换**真的被执行**
                    #     （A144 用无害 `touch PWNED` 实测，多出一个文件）
                    # 文件名来自仓库内容；仓库若来自第三方，这条路径可被构造。
                    # 用 shlex.quote 包住三个操作数。
                    #
                    # M157（A144，中危）：这条 cp 会**不可逆覆盖** `{rel}`，而
                    # 合并前的本地字节**只存在于 `{rel}` 这一个文件里** —— 脚本
                    # 就在紧邻的下一行刚说完「未另存合并前的本地副本」。实测：
                    # cp 之后本地改动在工作区 / .base / .remote / git HEAD
                    # 四处全部消失，且改动从未 commit 过。脚本对另一类不可逆
                    # 操作（删除）的态度是默认关闭 + 显式声明不可逆 + 要求单独
                    # 开启；这条同样不可逆的覆盖却只有一句中性的「整份用上游的」。
                    # 如实写明后果，不替用户决定。
                    print(f"     ! 下面这条 cp 会覆盖 {rel}"
                          f"（合并前的本地改动没有备份，不可逆）")
                    print(f"     整份用上游的：cp {shlex.quote(_rp)} "
                          f"{shlex.quote(rel)} "
                          f"然后 python3 push_api.py --resolve "
                          f"{shlex.quote(rel)}"
                          f"   （在仓库根目录执行）")
                    # 「整份保留我的」：本地文件里的原字节就是合并前
                    # 的那一份 —— 只要 `merged is not None` 才会被改写。
                    #
                    # N555（第一百八十二轮，中危）：上面这句原先**无条件**
                    # 打印，而 `_merge3` 有四处返回 `(None, True)`（二进制 /
                    # 超时 / git 不可用 / …），那时上面那道 `if merged is not
                    # None` 门控**根本没写文件** —— 本地一个字节没动：
                    #   · 「需手工删掉冲突标记段」→ 没有标记段可删（假）
                    #   · 「脚本未另存合并前的本地副本」→ 副本就是 `{rel}`
                    #     本身，完好无损（假）
                    # 两句都是假的，而下一行刚给出的 `cp` 恰好会**覆盖**
                    # 这份唯一副本 —— 用户被一句做不到的建议推向那条
                    # 会毁掉唯一副本的命令（与 A144 的 M157 叠加）。
                    #
                    # 实测（二进制冲突）：本地仍是原字节 True、含冲突标记
                    # False，而「整份保留本地」实际只需一条 `--resolve`。
                    if merged is None:
                        print(f"     整份保留本地的：python3 push_api.py "
                              f"--resolve {rel}")
                        print(f"     （本地文件未被改写，就是合并前的那份；"
                              f"无需删任何标记段）")
                    else:
                        # 「整份保留我的」**没有现成副本**：只落盘了远端与
                        # 合并基点，合并前的本地字节没有另存。如实说，
                        # 不给一条做不到的命令。
                        print(f"     整份保留本地的：需手工删掉冲突标记段"
                              f"（脚本未另存合并前的本地副本）")
            else:
                # N519（第一百七十一轮，中危）：「本次没有可比对的文件」
                # 把两样东西一起否掉了，而其中一样**就在本地文件里**。
                # 实测：副本写不出的那次 pull()，工作区 a.txt 已经是
                #   <<<<<<< 本地 / v3 / ======= / v2 / >>>>>>> 远端
                # —— 远端版本 v2 完整可见（在 `>>>>>>> 远端` 段）。
                # 真正缺的只有共同祖先（.base），而它是副本里唯一
                # 不可从冲突标记还原的那一份。
                #
                # 「没有可比对的文件」会让人合理地去远端网页上看、
                # 或凭印象手工合并。脚本在别处反复强调的是「依据是错的
                # 比没有依据更糟」—— 反过来，明明有依据却说没有，
                # 同样会让用户做出不必要的动作。
                # N512（第一百六十八轮，中危）：副本一个都没写出来，
                # 而 resolve() **从不检查副本是否存在** —— 它的两道校验
                #（成员资格：在不在 conflicts 里 / 冲突标记：改没改干净）
                # 都不覆盖「用户到底看没看到远端内容」这一维。
                # 于是副本缺失时它照常把基线补成远端当前值、照常推进
                # synced_commit，下一轮第一层「远端 == 基线」直接放行
                # → 远端那次改动被本地版本整文件覆盖，全程零提示。
                #
                # A8 修掉了「谎报副本已写出」，但那条谎报的**下游推理**
                # ——「既然副本已写出，用户必然已看过远端」—— 原样保留，
                # 危害只是从 pull() 搬到了 resolve()：那边是「没写成说成
                # 写成了」，这边是「没看到说成看到了」。
                # 把「本次确实没落盘副本」记进 state，让 resolve() 能区分。
                _cam = state.get("conflict_artifact_missing")
                if not isinstance(_cam, list):
                    _cam = []
                if rel not in _cam:
                    _cam.append(rel)
                state["conflict_artifact_missing"] = _cam
                # N631（接上）：用**真实成因**，不再写死成"目录不可用"。
                _why = _why_remote or _why_base or "副产物目录不可用"
                print(f"  ✗ {rel} 冲突：⚠ 冲突副本未能写出"
                      f"（{_why}）—— "
                      f"**远端版本**仍可比（在 {rel} 的冲突标记 "
                      f"`>>>>>>> 远端` 段），缺的是共同祖先副本（{rel}.base）。")
                # N518（第一百七十一轮，中危）：原先的出路是
                # 「可恢复目录后重跑 --pull」，而**照做必然空转** ——
                # 第一次 pull 已把 rel 写进 state["conflicts"]，而
                # pull() 循环开头是
                #   if not ignore_pending and rel in state["conflicts"]:
                #       print("上次冲突尚未解决…跳过")；continue
                # 于是"重跑 --pull"恰好被它上一轮自己留下的记录挡住，
                # 副本永远补不回来 —— 而补副本正是这次重跑的唯一目的。
                #
                # 三重静默：空转不报错、结论仍是"冲突 1 个"（像老样子）、
                # 用户手工改好后再跑 --resolve 会清掉记录，于是再跑
                # --pull 时 paths 为空，副本**还是**补不出来。
                #
                # 唯一能补出副本的是 `--pull --ignore-pending`（实测可行），
                # 但它在 USAGE 里挂在推送语义上、从没被接进出路文案。
                print("     要补出副本：恢复目录后跑 "
                      "python3 push_api.py --pull --ignore-pending")
                # N631（接上）：指引同样要按成因分流 —— 「恢复目录」只对
                # 成因①有效；成因②（磁盘满 / 只读挂载 / 配额）下目录此刻
                # 是好的，照做必然空转，等于又一条死路（与 N518 同形）。
                if "目录不可用" not in _why:
                    print("     （上面不是目录问题：先把磁盘空间 / 只读挂载"
                          " / 配额解决掉，再跑上面那条命令）")
                # N520（第一百七十一轮，中危）：而这条路有副作用，必须
                # 事先说明 —— 若该文件**已无冲突标记**（你已手工合并过），
                # --ignore-pending 会重新三方合并，把你手工合并的结果
                # **重新包回冲突标记**（内容不丢，但 --resolve 会再次
                # 以"仍有冲突标记"拒绝，手工合并得重做一遍）。
                print("     注意：若你已手工改好该文件，上面的命令会把你的"
                      "合并结果重新包回冲突标记；那种情况下副本只是不便，"
                      "直接 --resolve 即可，不必重跑。")
            if base_src is None:
                print("     合并基点未知：基线没记这个文件，本地 git HEAD 里也没有。"
                      "两侧都被当作新增，冲突未必是真冲突。")
            elif base_src == "git" and not base_ok:
                print("     基线没记这个文件，只有本地 git HEAD 里的版本；"
                      "而 HEAD 与你工作区一致，无法排除「你已 commit 了自己的改动」，"
                      "不能当共同祖先。")
                print("     故按「基点未知」处理：冲突未必是真冲突，"
                      + (f"请对照 {rel}.remote 人工判断。" if _w_remote
                         else "而本次没有可对照的远端副本，请谨慎判断。"))
                print("     （补上基线可避免此类误判："
                      "python3 push_api.py --init-baseline）")
            print(f"     手工改好后运行：python3 push_api.py --resolve {rel}")
            continue

        _write_local(rel, merged, rmode)
        state["files"][rel] = {"mode": rmode, "sha": rsha}
        clean.append(rel)
        print(f"  ✓ {rel} 已合并（{len(merged)} B）")

    # failed **不进** conflicts（见上面的说明）。
    #
    # N374（第一百一十九轮，中危）①：干净合并成功的文件必须**退出** conflicts。
    # 原先只做并集、从不做差集，于是 pull 合并干净并打印「✅ 可直接推送」
    # 后，记录却仍是 ['a.txt'] → 用户下次改这个文件被判「冲突尚未解决」
    # 退出码 3，而文件里根本没有冲突标记。
    # **脚本推荐的首选出路（--pull）本身制造了这个状态。**
    _conf_before = set(state.get("conflicts") or [])
    _merged_now = {safe_rel(c) for c in (clean or [])} - {None}
    _stale = sorted(r for r in _conf_before if safe_rel(r) in _merged_now)
    if _stale:
        print(f"  · {len(_stale)} 个文件本次已合并干净，"
              f"从冲突记录中移除：{_stale[:5]}")
    state["conflicts"] = sorted(
        (_conf_before | set(conflicted)) - set(_stale))
    # 判据是「没有冲突」，不是「有 clean」：clean 为空不代表没跟上 ——
    # 所有文件都走「远端无变化」分支时 clean 也为空，但本地确实已是最新。
    # 用 clean 做条件会让 synced 不刷新，用户被 P0-1 永久挡住（P1-2）。
    if _refuse_if_rewound(state, "--pull",
                          "   恢复后重跑 --pull。"):
        # 文件其实已经合并到本地了，所以 base_commit 照常推进（避免下次
        # 重复合并同一批），但**不写** synced_commit / local_head：
        # 那两行会抹掉回退证据，让后续推送绕过第零层。
        state["base_commit"] = head_sha
        save_state(state)
        return False
    # N97：原先只看 conflicted，不看 failed。
    # 于是"远端改动全部拉取失败（网络抖动/限流）、零冲突"时，
    # 照样写 synced_commit —— 声明"本地确实包含远端最新"，
    # 而实际上一个远端文件都没拿到。下一轮第一层校验看到
    # 这个声明会放行，改动进入"以为同步了其实没有"的状态。
    # 与行内注释（"本地确实包含远端最新"）和末尾 ✅ 文案直接矛盾。
    if failed or failed_base:
        print(f"\n  ! 有 {len(failed) + len(failed_base)} 个远端改动拉取失败，"
              f"**不**声明已同步（本地并未包含远端全部内容）。"
              f"稍后重跑 --pull 即可。")
    elif not conflicted:
        # N243：点名 `--pull <文件>` 时只合并用户点名的那几个，
        # 其余差异文件根本没进循环。而这里只看 failed / conflicted，
        # 不看 only —— 于是仍声明「本地已包含远端全部改动」。
        #
        # 后果：synced_commit 被推进到 head_sha，第一层校验从此认为
        # 「远端 == 基线」→ 那些没合并的文件**永久不再被拦**，
        # 下次推送会把它们的旧内容整文件覆盖回远端。
        # 这正是第一层存在的理由，一句话假声明就把它拆了。
        #
        # only 非空时：失败的、冲突的之外，还有**根本没处理**的。
        if only:
            # M77（A81，中危）**第二半**：这里同样用 `len(only)`（请求数）
            # 冒充成功数。它比下面那处更早打印，若不一起改，就会出现
            # 「先说合并了 1 个 → 再说一个都没合并成功」的自相矛盾
            # —— 又是同一段输出里两行打架（N346 定的性）。
            # 改说「处理了 N 个（其中 k 个合并成功）」，范围与结果分开报。
            print(f"\n  ! 本次只处理了点名的 {len(only)} 个文件"
                  f"（其中 {len(clean or [])} 个合并成功），"
                  f"其余远端改动未处理 —— **不**声明已同步。")
        else:
            state["synced_commit"] = head_sha      # 本地确实包含远端最新
            state["local_head"] = _local_head()
    state["base_commit"] = head_sha
    # local_seen：记录「本地曾经拥有过哪些路径」，删除功能的第三道判据。
    # 必须与基线刷新同点调用 —— 顺序反了会让刚推上去的文件在基线里
    # 却不在 local_seen 里，日后被删时判成「远端独有」而漏删。
    #
    # N242：`lmap` 是本函数**开头**取的快照，而循环里 _write_local
    # 已经把远端的新文件落盘了。用旧快照同步 → 本次刚拉下来的文件
    # 进不了 local_seen → 日后用户删掉它时判成「远端独有」而
    # **静默漏删**（文件留在远端，脚本从不提示）。
    #
    # 用**刷新后**的映射：改了工作区就要重新看，与 _write_local 里
    # invalidate_changes 的用意一致。
    _sync_local_seen(state, local_state_map()) if _sync_local_seen else None
    save_state(state)

    print(f"\n合并完成：干净 {len(clean)} 个，冲突 {len(conflicted)} 个"
          + (f"，拉取失败 {len(failed) + len(failed_base)} 个"
             if (failed or failed_base) else "")
          + (f"，因判为搬家而跳过 {len(moved_aside)} 个" if moved_aside else ""))
    if moved_aside:
        # N398：这些文件本地**确实没有**旧路径（只有内容相同的新路径），
        # 不计入 clean 之后必须在这里说清，否则"干净 N 个"读起来像
        # "全都拉回来了"，而 --scope 的 hint 恰恰把用户引到这条路上。
        print(f"  ! 以下 {len(moved_aside)} 个远端文件因判为搬家未拉回旧路径："
              f"{[(r, _moved_away.get(r)) for r in moved_aside[:10]]}")
        print("    它们在本地以新路径存在（内容相同），不是缺失；"
              "若要恢复旧路径，请手工从远端取回。")
    _report_remote_deletions(deleted_remote)
    # M74（A78）：`_gone_both` 在本支算出来了却从来不打印 —— 只有 paths
    # 为空那一条打了。而 paths 非空（远端既改了也删了）是更常见的场景，
    # 于是这类文件全程零行提到：用户不知道基线里还留着一条"两边都没了"
    # 的记录。两条返回路径必须同源，与 `_report_remote_deletions` 一致。
    _report_gone_both(_gone_both)

    if failed:
        # 必须给出**区别于冲突**的指引：重跑即可，不用 --resolve。
        print(f"  ! 以下 {len(failed)} 个文件因**远端当前内容**拉取失败未处理："
              f"{failed[:10]}")
        print("    多为网络抖动或限流，重跑 --pull 即可；"
              "它们**没有**被记为冲突，不需要 --resolve。")
    if failed_base:
        # N525：与上面分开打印 —— 这一类**远端当前内容是拿到了的**，
        # 取不到的是基线记录的那份历史版本（合并基点）。
        # 合并成一句「拉取远端内容失败」会让它对这一半为假。
        print(f"  ! 以下 {len(failed_base)} 个文件因**基线记录的历史版本**"
              f"取不到未处理（远端当前内容已取到）：{failed_base[:10]}")
        print("    重跑 --pull 即可；基线没被改写，不需要 --init-baseline。")
    if conflicted or failed or failed_base:
        if conflicted:
            print("冲突文件已记录，解决前不会被推送（避免把冲突标记推上去）。")
            print("逐个解决：python3 push_api.py --resolve <文件>")
            print("（本地已删掉的那些要加 --yes："
                  "python3 push_api.py --resolve <文件> --yes）")
        return False

    # 这里**故意不自动 git commit**：一旦提交，工作区就"干净"了，
    # detect_changes() 再也看不到这些合并结果 —— 用户想把自己的改动
    # （连同刚合进来的远端改动）推上去时，脚本只会说「本地没有待推送的改动」。
    # 保持工作区脏，合并结果才推得出去；是否落本地提交交给推送流程统一处理。
    # N346（第一百〇八轮，中危）：N243 只修了**判据**（不写 synced_commit），
    # 没修**告知**。实测（远端 a.txt/b.txt 都变过，只跑 `--pull a.txt`）：
    #
    #     ↓ a.txt 本地无改动，已更新到远端版本
    #     ! 本次只合并了点名的 1 个文件，其余远端改动未处理
    #       —— **不**声明已同步。
    #     ✅ 本地已包含远端 bbbbbbbb 的全部改动，可直接推送。   ← 假的
    #
    # **同一份输出里，上一行说"其余远端改动未处理"，下一行说"已包含
    # 全部改动"。** 而 pull() 仍返回 True（调用方视为成功）。
    #
    # 形态最坏：不是"少说一句"，而是**与同一段输出里上一行的真话直接
    # 打架**，用户读到的是"脚本自己前后矛盾"，无法判断该信哪句。
    #
    # N243 修的是决定性的一半（保住了第一层 Guard 的兜底），所以这不是
    # 数据丢失、定中危；但告知层的假声明必须一起修。
    if only:
        # M77（A81，中危）：`len(only)` 是**请求**的文件数，不是**合并成功**的数。
        # 真正合进来的是 `clean`。实证（`--pull typo.txt`，远端无此文件）：
        #
        #     合并完成：干净 0 个，冲突 0 个                        ← 真话
        #     ✅ 本次只合并了点名的 1 个文件（远端 bbbbbbbb）…       ← 与上一行直接打架
        #
        # **一个都没合并，却带 ✅ 报"已合并 1 个"。**
        #
        # 与 N346 同一形态、方向相反：N346 修的是「部分却声称全部」，
        # 这里是「一个都没成，却带 ✅ 报成功数」。两处都在 `if only:` 分支、
        # 用的是同一个变量 —— N346 只补了「告知在不在」，没改这个数字。
        #
        # 三场景对照（补丁前 → 补丁后）：
        #     点名 typo.txt          0 成功  → ✅ 1 个 ★  →  ! 一个都没合并成功
        #     点名 a.txt             1 成功  → ✅ 1 个     →  ✅ 1/1
        #     点名 a.txt typo.txt    1 成功  → ✅ 2 个 ★  →  ✅ 1/2
        _n_req, _n_ok = len(only), len(clean or [])
        # M137（A126，低危）：M77 把这个数字从「点名的数量」改成了
        # 「实际合并的数量」，但**差额没有交代** —— 场景 C（点名
        # `X.txt` + `../etc/passwd`）打印「1/2」，用户只知道少了一个，
        # 不知道少的那个是**被拒了**还是**远端没有**。
        #
        # 而 A5-I1 已经算出了 `_rejected_only`（它只在「全部被拒」那一支
        # 用到了），这里顺手列出即可 —— 差的不是判据，是把已有事实说出来。
        #
        # 不说清的代价：被拒的路径在**开头**打印过一次「忽略越界路径」，
        # 但收尾这句「1/2」离它很远，读起来像"合了 1 个、还剩 1 个待处理"，
        # 而"其余远端改动未处理"指的是**别的**差异文件，不是被拒的那个。
        #
        # N574 / N575（第一百八十八轮，中危）：M137 只补了 `_rejected_only`
        # 这一类，而能走到本收尾的「本次没合并」成因共有**三类**
        # （去掉提前 return 的 failed / conflicted）：
        #   越界被拒 `_rejected_only` / 判为搬家 `moved_aside` /
        #   远端已删除 `deleted_remote`（外加"远端也没有"这一类）。
        #
        # 于是：点名一个被判搬家的文件 → 上方三处说「搬家，脚本主动跳过，
        # 这是预期结果」，收尾说「常见原因是路径拼写或该文件在远端不存在」
        # —— **同一份输出里三条归因逐条不成立**（路径拼写正确、远端存在、
        # 也不是失败）。点名一个远端已删的文件同样被归因成「路径拼写」。
        #
        # 更重的一半是它把用户推进循环：收尾说「核对路径后重跑」，
        # 而 A126 实测连跑三次输出一字不变、退出码恒 1 —— 用户没有做错
        # 任何事。所以归因必须按**实际成因**分流，且「无 failed、无
        # conflicted」时不能再按失败返回。
        #
        # 只统计**本次点名**里那几条，不用全局的 moved_aside /
        # deleted_remote（它们在非点名 `--pull` 下还含别的路径）。
        _only_rels = {_r for _r in (safe_rel(_f) for _f in (only or [])) if _r}
        _mv_in = [p for p in (moved_aside or []) if p in _only_rels]
        _del_in = [p for p in (deleted_remote or []) if p in _only_rels]
        _rej_n = len(_rejected_only or [])

        def _cause_bits():
            """按实际成因列举差额（返回 [(文案, 是否属"失败")]）。"""
            bits = []
            if _rej_n:
                bits.append((f"{_rej_n} 个被判越界/非法、**从未参与合并**："
                             f"{_rejected_only[:5]}"
                             f"{' …' if _rej_n > 5 else ''}"
                             f"（它们是路径问题，重跑 --pull 也不会变）",
                             True))
            if _mv_in:
                bits.append((f"{len(_mv_in)} 个因判为搬家而**主动跳过**"
                             f"（上方已列出：本地以新路径存在，不是缺失；"
                             f"重跑 --pull **不会**拉回旧路径）", False))
            if _del_in:
                bits.append((f"{len(_del_in)} 个**远端已删除**"
                             f"（上方已列出：本地仍保留，本脚本不代劳删除）",
                             False))
            _known = _rej_n + len(_mv_in) + len(_del_in)
            _gap = _n_req - _n_ok - _known
            if _gap > 0:
                bits.append((f"其余 {_gap} 个是远端没有该文件或无改动", True))
            return bits

        if _n_ok == 0:
            _bits = _cause_bits()
            if any(not _f for _t, _f in _bits):
                # 有「不是失败」的成因 → 不能再用「路径拼写 / 远端不存在」
                # 这句固定归因，也不该按运行错误返回。
                print(f"\n  ! 点名的 {_n_req} 个文件**一个都没合并成功**"
                      f"（远端 {head_sha[:8]}）—— 上方已说明各自原因，"
                      f"**不是**路径拼写错误、也不是远端没有这些文件：")
                for _t, _f in _bits:
                    print(f"     · {_t}")
                print("   它们既不是冲突也不是拉取失败 —— "
                      "重跑 --pull 结果不变（搬家 / 远端已删除 都不是错误）。")
                # 返回值：点名的每一条都能被「非失败」成因解释 → 不是错误。
                # 只要有未被解释的（越界 / 远端没有），仍按失败返回，
                # 让调用方给退出码 1。
                if not any(_f for _t, _f in _bits):
                    return True
                return False
            print(f"\n  ! 点名的 {_n_req} 个文件**一个都没合并成功**"
                  f"（远端 {head_sha[:8]}）—— 上面「干净 0 个」已经说明，"
                  f"常见原因是路径拼写或该文件在远端不存在。")
            print("   **不**声明已同步；核对路径后重跑。")
            # 返回值同理：点名的那几个一个都没成，就不是成功。
            # 调用方据此给退出码 1（无冲突时）而不是 0 ——
            # 否则「一个都没成」与「全部成了」在 CI 眼里没有区别。
            return False
        print(f"✅ 本次只合并了点名的 {_n_ok}/{_n_req} 个文件"
              f"（远端 {head_sha[:8]}），其余远端改动**仍未处理**：")
        print("   再跑一次不带参数的 --pull 才会把它们合进来。")
        _bits = _cause_bits()
        if _bits:
            # N575：原来只在 `_rejected_only` 非空时才交代，并以一句
            # 「其余差额是远端没有该文件或无改动」盖住 —— 而这句话对
            # 搬家 / 远端已删除那两类是**假的**（远端有这个文件，且
            # 上方刚刚列出了它）。改成逐成因列举，不再有盖住的那句。
            print(f"  ! 这 {_n_req - _n_ok} 个差额是：")
            for _t, _f in _bits:
                print(f"     · {_t}")
        # 返回值同理：调用方按"成功"理解会误以为可以推送。
        # 但对**点名**的那几个确实是成功的，所以返回 True 而**不**声明
        # 全局已同步 —— 判据与告知同源（都看 only），不会再打架。
        return True
    print(f"✅ 本地已包含远端 {head_sha[:8]} 的全部改动，可直接推送。")
    return True


def resolve(state, rel, head_sha=None, rstate=None, yes=False):
    """确认某个冲突文件已手工解决：清冲突标记 + 补基线。

    调用前会检查文件里是否还留着 `<<<<<<<` 标记——带着冲突标记推送等于
    把一份损坏的文件推上去，这是最该拦的一道。

    手工解决意味着人已经看过远端版本并做了决定，所以这里把基线补成远端当前值：
    下一轮第一层校验看到「远端 == 基线」即放行，本地（人工裁定的）内容可以推上去。
    全部冲突解决完则声明「本地已包含远端最新」，解除 P0-1 过期拦截——
    否则用户解决完所有冲突仍被拦在门外，只能去加 --force-overwrite。
    """
    # 成员资格校验：不在冲突列表里的文件不该走到这里。
    # 副作用是**刷 synced_commit**（等价于声明「本地已含远端最新」），
    # 对一个从没冲突过的文件做这个声明，等于无凭无据地解除 P0-1 闸门 ——
    # 那正是防静默回退的最后一道。打错文件名时尤其危险。
    if rel not in (state.get("conflicts") or []):
        pending = state.get("conflicts") or []
        raise SystemExit(
            f"{rel} 不在未解决冲突列表里。\n"
            + (f"  当前待解决的是：{pending}\n" if pending
               else "  当前没有任何未解决冲突。\n")
            + "  --resolve 会声明「本地已含远端最新」，"
              "只能用于真实冲突过的文件。")
    data = _local_bytes(rel)
    if data is None and os.path.exists(os.path.join(ROOT, rel)):
        # N629（第一百六十九轮，中危）：`_local_bytes` 的 None 是「不存在」
        # 与「读不到」的**合并值**（open 抛 OSError 一律 return None），
        # 而下面那句提示把它单向解释成「本地不存在」。
        # 实测（`x.txt` 是个目录）：
        #   os.path.exists('x.txt') = True   ← 条目确实在
        #   open() 抛 IsADirectoryError（OSError 子类）
        #   _local_bytes('x.txt')   = None   ← 与「不存在」同一个 None
        # 于是提示说「本地不存在」、并建议用 `--pull` 把它拉回来 ——
        # 而成因是读不到，拉回来照样读不到，方向是错的。
        # 脚本在多处反复立过「无法判断 ≠ 不存在」这条原则
        #（N364 / N455 / N456），这一处没跟上。
        # 先用 os.path.exists 把两者分开：条目在但读不到时，
        # **不给**「放弃改动、保留远端」这个选项 ——
        # 因为无法判断用户要放弃的是哪一份，此时替他决定等于押注。
        raise SystemExit(
            f"{rel} 无法读取，**不是**「本地不存在」：该条目存在，"
            f"但打开失败。\n"
            f"  常见成因：它是目录、权限不足、或路径过长。\n"
            f"  本命令要读本地内容才能判断你是否已改好，"
            f"  请先手工确认该条目的实际形态再重跑。")
    if data is None:
        # N374（第一百一十九轮，中危）②：文件被删 = 用户放弃这次改动。
        # 原先直接 SystemExit，于是四条命令没有一条能清记录：
        #   --clean-conflicts  只删副产物文件，不动这条记录
        #   --resolve          ← 就是这里，必然失败
        #   --mark-synced      被「还有 1 个未解决冲突」拒绝
        #   --reset-baseline   N228 判据下 conflicts 保留
        # 结果 --status --json 永久显示这个冲突，推送永久被拦。
        # 改成把它当成一种**有后果的选择**，而不是错误。
        if not yes:
            raise SystemExit(
                f"{rel} 本地不存在。\n"
                f"  它仍在冲突记录里，而没有文件可供检查 —— "
                f"这通常是「放弃这次改动，保留远端那份」。\n"
                f"  后果：本地改动丢弃，远端文件**保留**（不会被删）。\n"
                f"  确认要这样，请加 --yes："
                f"python3 push_api.py --resolve {rel} --yes\n"
                f"  不确认的话，用 --pull 把它拉回来："
                f"python3 push_api.py --pull")
        state["conflicts"] = [p for p in (state.get("conflicts") or [])
                              if p != rel]
        # N514（第一百六十九轮，中危）：上面那句提示承诺
        # 「远端那份**保留**（不会被删）」，而本分支既没有把 rel 从
        # state["files"] 移除、也没有从 local_seen 移除 —— 这两项正是
        # 删除判据 3「本地曾拥有 + 本地现在没有 + 远端有」的两个输入。
        # 实测：resolve --yes 之后跑删除管线，rel 照样进 entries
        #（sha=None 即删除条目），带 --delete 推送时，那个刚被承诺
        # **不会被删**的远端文件会被删。
        # 删除默认关闭（_ENABLE_DELETE = False）所以定中危而非高危，
        # 但用户正是照着「远端保留」这句才敢选放弃改动 ——
        # 提示与行为直接相反，比没有提示更糟。
        #
        # 落实方式：只从 local_seen 移除 —— 判据 3 不再成立，rel 就不会
        # 被判成「本地删除」。state["files"] **保留**：远端确实还有这份，
        # 基线得继续记着它，否则下次它会被当成「远端新增」再报一次。
        _ls = state.get("local_seen")
        if isinstance(_ls, list) and rel in _ls:
            state["local_seen"] = [p for p in _ls if p != rel]
        # N552（第一百八十一轮，中危）：上面只做了「不再判成本地删除」，
        # 基线却仍记着本地旧版 —— 而本分支刚刚把 synced_commit 推进到
        # 远端 head，于是 --status 同一份输出里「本地副本：已同步」与
        # 「落后于远端的文件：1 个」并存，两句指向相反的动作。
        # 更实质的是：--status 给的下一步是 --pull，而 --pull 会把远端
        # 那份写回本地工作区 —— 用户刚刚用 --yes 明确表达的「放弃改动、
        # 保留远端那份」被撤销，他的裁定白做。
        # 与文件还在的那一支（同样写 state["files"][rel] = rstate 值）
        # 同源：记成远端值是准确的，远端确实还有这份，也不会被当成
        # 「远端新增」再报一次。
        if rstate and rel in rstate:
            state["files"][rel] = {"mode": rstate[rel][0],
                                   "sha": rstate[rel][1]}
        # N632（A130 的 M140，低危）：`--resolve` 只**删**副本、从不写副本
        # （实测：目录不可用时照常 `✅ 已标记为解决`）。而 `_conflict_path`
        # 在目录不可用时会在自己内部打一句
        #   「! 冲突副产物目录不可用（…），本次不写副本；
        #     冲突内容只能从远端重新获取。」
        # 于是这次操作压根不需要副本，却先看到这么一句，且它排在成功结果
        # **之前** —— 容易被读成"有什么失败了"。
        #
        # 目录不是目录时副本必然不存在，也没有东西可删，直接跳过整段：
        # 既不触发那句提示，也不白白调一次 makedirs。
        if os.path.isdir(CONFLICT_DIR):
            for kind in ("remote", "base"):
                extra = _conflict_path(rel, kind)
                if extra and os.path.exists(extra):
                    os.unlink(extra)
        save_state(state)
        print(f"  · {rel} 本地已删除 —— 视为放弃改动，"
              f"已从冲突记录移除（远端那份保留）")
        if head_sha and not state["conflicts"]:
            # N628（第一百六十九轮，高危）：全脚本 6 处 _refuse_if_rewound
            # 调用点里，这里是**唯一**裸调用、不接返回值的 ——
            # 闸照跑、完整的拒绝理由照打印，随后同一函数紧接着就写
            # synced_commit / local_head，把回退证据当场抹掉。
            # 实测（本地 git reset --hard 回退后走本分支）：
            #   ❌ 本地 git HEAD 发生变化…此时声明「已同步」会让
            #      本地旧内容把远端静默覆盖掉。  ← 闸自己打印的
            #   · 全部冲突已解决，标记为已同步      ← 紧接着就做了
            # 两条输出自相矛盾；且 local_head 被改写成回退后的 HEAD 之后，
            # _local_head_rewound 的判据（merge-base --is-ancestor）
            # 从此恒真 —— **永久失效**，没有任何一次后续运行能再发现
            # 这次回退。这正是脚本自述的 P0 根因（静默回退主干）。
            #
            # 与主分支那处（`if not _refuse_if_rewound(...)`）同形；
            # 同样**不用 early return**：下面的 save_state 要照跑，
            # 否则「冲突标记已清」这个结果不落盘。
            if not _refuse_if_rewound(state, "--resolve",
                                      "   恢复后重跑 --resolve。"):
                state["synced_commit"] = head_sha
                state["local_head"] = _local_head()
                print("  · 全部冲突已解决，标记为已同步")
        return True
    # N225：只查 <<<<<<< 与 >>>>>>>，漏了 =======。
    # 而 _merge3 用 `git merge-file -L 本地 -L 基线 -L 远端` 生成的
    # 三种标记**都是 7 字符**（实测）。只删掉头尾两个标记、
    # 中间的 ======= 还留着，本检查照样通过 ——
    # 于是带着残缺内容的文件被推上远端。
    #
    # N233：但直接子串查 `=======` 会**误伤 5 类正常文件** ——
    #   · Python 注释分隔线 `# =======`
    #   · Markdown setext 标题（`标题\n=======`）
    #   · YAML / ini 分隔线
    #   · 对齐用的等号串
    #   · diff 输出里的分隔线
    # 这些文件被判「仍有冲突标记」→ --resolve 拒绝 → **推送永久阻塞**，
    # 而用户完全没法解决（文件里根本没有冲突）。
    #
    # 正确判据：git merge-file 生成的标记必定**独占一行且在行首**。
    # 按行判，且只有 `<<<<<<<`/`>>>>>>>` 命中才算冲突
    # （`=======` 单独出现不构成冲突 —— 它没有配对的头尾）。
    # N288：走到这里的文件**必然在 state["conflicts"] 里**（上方
    # `if rel not in ...` 已挡掉其余），即**已知有冲突史** ——
    # 所以 strict=True：行首 `=======` 也认。
    _has, _line = _conflict_marker_hit(data, strict=True)
    if _has:
        raise SystemExit(
            f"{rel} 里仍有冲突标记（<<<<<<< / ======= / >>>>>>>）。\n"
            f"  命中的那一行：{(_line or '')[:80]!r}\n"
            f"  若是残留的 git 分隔线 —— 删掉它再重跑。\n"
            f"  若它本来就是文件的正常内容（Markdown setext 标题、"
            f"YAML/ini 分隔线都可能写成这样）——\n"
            f"    改一下那一行（比如换个符号或换个长度），再重跑。\n"
            f"  把命中的原文打出来，就是为了让你不必猜："
            f"N233 那次「提示让人去改一个不存在的冲突」不能再发生。"
        )
    # N632（接上）：同 `--resolve` 里那一处 —— 这里同样是删副本而不是写
    # 副本，目录不可用时不该先打那句「本次不写副本」。
    if os.path.isdir(CONFLICT_DIR):
        for kind in ("remote", "base"):
            extra = _conflict_path(rel, kind)
            if extra and os.path.exists(extra):
                os.unlink(extra)
    state["conflicts"] = [p for p in (state.get("conflicts") or []) if p != rel]
    # N512（第一百六十八轮，中危）：副本缺失时，下面这两行的**前提**
    # ——「人已经看过远端版本才做的裁定」（见 resolve 的 docstring）——
    # 从未被校验过。副本一个都没写出来时，resolve 照常补基线、
    # 照常推进 synced_commit，下一轮第一层「远端 == 基线」随即放行，
    # 远端那次改动被静默覆盖，链路里没有任何一处说「你这次没看到远端」。
    #
    # pull() 已把「本次没落盘副本」记进 conflict_artifact_missing（N512）。
    # 这里据此分两种处置：
    #   · 无 --yes → 拒绝，并给出真正能补出副本的那条路
    #     （--pull --ignore-pending，见 N518/N520）；
    #   · 有 --yes → 照办，但**如实标注**这是用户的确认而非脚本的核实
    #     —— 不谎报，也不把用户堵死。
    _missing_artifact = rel in (state.get("conflict_artifact_missing") or [])
    if _missing_artifact and not yes:
        raise SystemExit(
            f"{rel} 本次冲突**没有落盘过远端副本**（副产物目录不可用），"
            f"无法确认你是否看过远端内容。\n"
            f"  --resolve 会把基线补成远端当前值，等于声明"
            f"「本地已包含远端最新」，下一轮校验随即放行 ——\n"
            f"  若你其实没看过远端，这次裁定会静默覆盖它。\n"
            f"  请先比对远端：内容就在工作区 {rel} 的冲突标记 "
            f"`>>>>>>> 远端` 段里（缺的只是共同祖先副本）。\n"
            f"  确认已看过，请加 --yes 重跑。\n"
            f"  想先拿到副本：恢复目录后跑 "
            f"python3 push_api.py --pull --ignore-pending")
    if rstate and rel in rstate:
        state["files"][rel] = {"mode": rstate[rel][0], "sha": rstate[rel][1]}
        print(f"  · {rel} 基线已补记为远端当前版本（你的裁定内容会在推送时覆盖远端）")
        if _missing_artifact:
            print(f"  ⚠ 本次无远端副本可比 —— 上面这句「已包含远端最新」"
                  f"来自你的 --yes 确认，**不是**脚本核实的结果。")
    _cam2 = state.get("conflict_artifact_missing")
    if isinstance(_cam2, list) and rel in _cam2:
        state["conflict_artifact_missing"] = [p for p in _cam2 if p != rel]
    if head_sha and not state["conflicts"]:
        # N95：全脚本有 6 处写 synced_commit / local_head，
        # 这里是唯一**没过** _refuse_if_rewound 的一处。
        # 而那道闸的不变量写得很明确（见其 docstring）：
        # 「必须在写 synced_commit / local_head **之前**调用，
        #   那两行一写，回退证据当场抹掉，第零层从此失效」。
        # resolve 同样在"声明已同步"，同样会抹掉证据 —— 缺了它，
        # 用户在本地 reset 后再 --resolve，第零层就被悄悄绕过了。
        # 不用 early return：下面还有 _sync_local_seen 与 save_state，
        # 提前返回会让"冲突标记已清、基线已补"的结果**不落盘**，
        # 下次运行一切照旧 —— 用户以为解决过了。
        #
        # N350（第一百一十轮，低危）：`#95` 补上这道闸时**没带 extra**，
        # 于是 --resolve 成了四条路径里唯一"被拦下却不说恢复完该干什么"的
        # —— 用户拿到 reflog / reset 两步，照做完之后不知道下一步是什么。
        # 与 --pull / --mark-synced 那三处同形，补齐即可。
        if not _refuse_if_rewound(state, "--resolve",
                                  "   恢复后重跑 --resolve。"):
            state["synced_commit"] = head_sha
            state["local_head"] = _local_head()
            print("  · 冲突已全部解决，本地视为已包含远端最新")
        state["base_commit"] = head_sha
    # N38：resolve 是第 6 个基线刷新点，此前同样缺 local_seen 同步。
    # 冲突解决后该文件已确认归本地所有，却没被记进「曾经拥有」清单 ——
    # 日后删除它会因判据 3 被判成远端独有而静默漏删。
    if _sync_local_seen:
        try:
            # N115：sync_local_seen 返回"被非 UTF-8 过滤掉的路径数"
            #（docstring 写明是给调用方提示用的，N7），
            # 但**两侧都没接线**：6 个调用点全部丢弃返回值，
            # report_removals 的 skipped_non_utf8 参数也从未被传入。
            # 于是"非 UTF-8 路径已排除"这句提示是死代码 ——
            # 而这类路径被静默丢弃正是删除功能最该说清楚的事。
            _dropped = _sync_local_seen(state, local_state_map())
            if _dropped:
                print(f"  ! {_dropped} 个非 UTF-8 路径无法记入"
                      f"「本地曾拥有」清单，日后删除它们时会被"
                      f"判成远端独有而跳过。")
        except Exception as _e:
            print(f"  ! local_seen 同步失败：{type(_e).__name__}: {_e}")
    # L2（A8）建议把批量 --resolve 的 N 次 save_state 合并成循环外一次，
    # 以减小写放大。**不采纳**，且这里记下理由以免下一个人照着改：
    #   · 每次 resolve 的产出（清冲突标记 + 补基线）是**独立的裁定结果**，
    #     逐个落盘时第 k 个失败只丢第 k 个；合成一次则前 k-1 个裁定一起丢。
    #   · A8 自己举的场景正是「中途 typo.txt 不在冲突列表 → 中止」，
    #     那时逐个落盘把已完成的两个保住了；合成一次会全部作废，
    #     用户重跑还得重新裁一遍已经裁过的文件。
    # 写放大的代价（慢盘上多几次原子写）小于丢裁定的代价。
    save_state(state)
    print(f"✅ {rel} 冲突已标记为解决")


def scope_classify(state, head_sha, rstate, lmap=None):
    """三方全景分类：把「谁动了什么」一次性列全。

    为什么要有这个视图：MINE / THEIRS / BOTH 三类的判据在本脚本里
    **早就有了**，但分散在三处、各算各的 ——

      · MINE     detect_changes() / local_state_map()（推送侧待办）
      · THEIRS   pull() 的 cand 判据（远端变过 + 本地没合上）
      · BOTH     mark_synced() 的 unmerged 判据

    三处判据的分母各不相同（一个看工作区、一个看 rstate、一个看
    state["files"]），于是没有任何一个出口能回答「这 N 个文件各自归谁」。
    使用者只能分别跑 --status / --pull 再自己在脑子里做交集 —— 而交集
    恰恰是最容易漏的一步：漏了没有报错，只是某个文件静默停在错的类里。

    本函数只做**一次**三方比对，分母统一为「远端 ∪ 本地 ∪ 基线」。

    判据（l / r / b 分别是本地 / 远端当前 / 基线的 (mode, sha)）：

        l 无 + r 有            REMOTE_ONLY  远端有、本地缺 → --pull 补齐
        l 有 + r 无            LOCAL_ONLY   本地有、远端无 → 推送（新增）
        l == r                 SAME         已一致
        b 缺失（基线没记）      UNKNOWN     无法判断 → 拦下，不推
        l 变 + r 变            BOTH         两边都动 → 人工 / --pull 三方合并
        l 变 + r 未变          MINE         只有我动 → 推送
        l 未变 + r 变          THEIRS       只有远端动 → --pull

    **UNKNOWN 为什么单独成类、不并入 MINE**：
    基线没记该文件时，「远端当前版本是别人改的还是我上次推的」无从判断，
    此时声称「是我改的、可以推」是 fail-open。init_baseline() 的 docstring
    立的正是 opposite 的原则：本地 ≠ 远端的文件**不给它记基线**，好让第一层
    校验把它拦下来强制先同步。分类表若在这里把它归成「可推送」，等于
    用一张只读的表格悄悄放行了写路径上被拦住的东西 —— 表格比命令更容易
    被当真（它是"看一眼现状"的入口）。宁可多一类，不可多放行。

    ## 基准取哪一列：state["files"]（增量快照），**不是** tree_of(base_commit)

    这是本函数最容易被人"顺手修正"错的一处，故把实测结论记在这里。

    外部同类工具（derive_scope.py 一系）的做法是：基准 = 从
    `base_commit` 那次提交**重新拉一棵树**，理由是它权威、自洽，
    不依赖增量维护是否严格。那个理由本身成立，但**用在本脚本上是错的**，
    因为两者的 base_commit 语义不同：

      · 外部工具：base_commit = "我上次同步到的远端提交"。
        它的整棵树就是"我本地所基于的版本"，取树即得基准。自洽。
      · 本脚本：base_commit 在推送成功后被推进到**本次新产生的提交**
        （见 _main 的 `state["base_commit"] = commit["sha"]`）。
        那棵新树里，别人先推的改动已经在里面了。

    于是对**一个远端已改、本地还没合上的文件 X**（即 pull 里的 lagging）：

        实测三元组       本地 l      远端 r      基线 b
        ------------------------------------------------------
        state["files"]   v1          v2          v1  → THEIRS ✅
        tree_of(base)    v1          v2          v2  → MINE   ❌

    后者会把"别人改的、我还没合上的"判成"我改的、可以推" —— 正是
    README 坑 1 描述的那个事故（"10 个别人推的文件全被判成我改的"），
    只不过触发方式不是手填 base，而是换了个基准取法。

    根因：本脚本的 `files` 记的是**"我本地所基于的内容版本"**，
    对未合并的文件它**故意不刷新**（推送主循环里那类进 `lagging` 而非
    `refreshed`），这正是三方合并需要的那一列。base_commit 推进而
    files 不推进，看起来像"不一致"，其实是有意的分工 —— 两者回答的
    不是同一个问题：base_commit 回答"我的分支从哪长出来"，
    files 回答"我手上这份内容是从哪改过来的"。合并判据要的是后者。

    代价与兜底：`files` 是增量维护的，理论上会漂移。故本函数额外把
    `base_entries`（基线实际条数）一并返回，并在 UNKNOWN 里收下
    "基线记的 sha 与两边都不同"那一格 —— 那正是漂移能被看见的地方。
    若日后要加自检，应比对 `files` 与 `tree_of(base_commit)` 的差集，
    **而不是**直接改用后者。

    返回 dict，不打印、不写状态（纯计算，可单测）。
    """
    if lmap is None:
        lmap = local_state_map()
    base = state.get("files") or {}

    groups = {"MINE": [], "THEIRS": [], "BOTH": [], "LOCAL_ONLY": [],
              "REMOTE_ONLY": [], "UNKNOWN": []}
    same = 0
    for p in sorted(set(rstate) | set(lmap) | set(base)):
        l = lmap.get(p)
        r = rstate.get(p)
        b = base.get(p)
        b_sha = b.get("sha") if isinstance(b, dict) else b
        if l is None and r is None:
            # 只在基线里、两边都已不存在：归类无意义，跳过。
            # （这类条目由 pending_deletes_union 负责，不在这里重复计数，
            #   否则同一个文件在 --status 与 --scope 里出现两种口径 ——
            #   正是本仓库反复修的「同一语义多处实现」。）
            continue
        if l is None:
            groups["REMOTE_ONLY"].append(p); continue
        if r is None:
            groups["LOCAL_ONLY"].append(p); continue
        if l == r:
            same += 1; continue
        # 到这里：本地与远端不一致，必须靠基线判是谁动的
        if not b_sha:
            groups["UNKNOWN"].append(p); continue
        # N400（第一百二十九轮，中危）：这里只比 sha、不比 mode。
        #
        # 于是「只改了权限位、内容一字未改」在两侧都落进 UNKNOWN：
        #   远端 chmod → 应为 THEIRS，落到 UNKNOWN
        #   本地 chmod → 应为 MINE，  落到 UNKNOWN
        #
        # 第二条更糟：--scope 说「无法判定归属 → 不推」，而同一份状态下
        # detect_changes() **含**这个文件 —— 推送侧认为它是待推送改动，
        # 且那个判断才是对的。两个命令对同一件事结论相反。
        #
        # 同时，UNKNOWN 的 hint 说「基线没记录」也是假话：基线里明明白白
        # 记着它（只是判据不比 mode）。真实原因与提示的成因不同，
        # 照 hint 跑 --init-baseline 实测仍为 UNKNOWN。
        #
        # 修法：mode 与 sha 一起比。基线无 mode（旧形态裸 sha）时退化为
        # 只比 sha —— 那种情况下猜 mode 没有依据。
        # 这与 FileBaselineGuard「mode + sha 一起比」是同一条判据。
        b_mode = b.get("mode") if isinstance(b, dict) else None
        if b_mode:
            l_changed = (l != (b_mode, b_sha))
            r_changed = (r != (b_mode, b_sha))
        else:
            l_changed = (l[1] != b_sha)
            r_changed = (r[1] != b_sha)
        if l_changed and r_changed:
            groups["BOTH"].append(p)
        elif l_changed:
            groups["MINE"].append(p)
        elif r_changed:
            groups["THEIRS"].append(p)
        else:
            # 基线记的 sha 与两边都不同 —— 基线本身已漂移（旧形态、
            # 或别处漏刷）。此时硬归入 MINE/THEIRS 都是猜。
            groups["UNKNOWN"].append(p)
    groups["SAME"] = same
    return {
        "head": head_sha,
        "base_commit": state.get("base_commit"),
        "synced_commit": state.get("synced_commit"),
        # base_entries：基线实际记了多少条。它是本表判据的**分母**，
        # 必须一并给出 —— 否则「UNKNOWN 3 个」读者无从判断那是
        # 「仓库 830 个文件里的 3 个」还是「几乎全部」。
        "base_entries": len(base),
        **groups,
    }


#: 各类的人类可读说明与建议动作。放在模块级而不是塞进渲染函数里：
#: 文本与 JSON **共用同一份**，避免两处文案漂移（N173 的同源要求）。
SCOPE_HINTS = {
    "MINE": "只有你改过 → 直接推送（python3 push_api.py <文件>）",
    "THEIRS": "只有远端改过 → 先 --pull 合进本地，别直接推",
    "BOTH": "两边都改过 → --pull 做三方合并，冲突则 --resolve",
    "LOCAL_ONLY": "远端还没有 → 推送即新增",
    "REMOTE_ONLY": ("本地缺这些文件 → 两种可能，**动作相反**：\n"
                    "                  ① 远端新增的 → --pull 补齐；\n"
                    "                  ② 你本地刚删/移走的 → --pull 会**把它们拉回来**"
                    "（撤销你的重构）\n"
                    "                  区分：基线里本地曾有过即 ②；先 --scope 看清再决定"),
    # N400：原写「基线没记录」—— 那是假话。UNKNOWN 实际有两种成因，
    # 二是「基线记了，但它记的 mode+sha 与本地、远端**都**不同」
    #（基线本身已漂移），此时硬归入 MINE/THEIRS 都是猜。
    # 只写第一种成因会把人推向 --init-baseline，而实测那样跑完仍是
    # UNKNOWN（真实原因不是"没记录"）。
    "UNKNOWN": ("基线缺失或已漂移（它记的版本与本地、远端都不同）"
                "→ 无法判定归属，不推；先 --pull，仍不行再 --init-baseline"),
}


# ---------------------------------------------------------------- PR 工作流

def _now():
    """当前 UTC 时间，格式与 GitHub 返回的时间戳一致。

    必须 UTC：_days_since() 按 UTC 解析 GitHub 的 `merged_at` 等字段，
    而旧版这里用 time.strftime 写的是**本地时间**。同一份 state 里两种
    口径混用，非 UTC 环境下天数会偏若干小时，直接影响 --prune 的
    3 天宽限判定（该报的不报，不该报的报了）。
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _days_since(ts):
    """时间戳 → 距今天数。解析不了返回 None（只影响提示，不阻断）。"""
    if not ts:
        return None
    # 偏移里的 "+" 和 "-" 都要剥。原来只 split("+")，遇到负偏移
    # （2026-09-01T12:00:00-05:00）会把 "-05:00" 留在字符串里，
    # strptime 全部失败 → 返回 None → 该分支静默归入「无需处理」，
    # --prune 就不报它了（本该报告的陈旧分支消失）。
    #
    # 用正则先切掉尾部偏移再剥小数秒，避免 "2026-09-01" 里的日期连字符
    # 被误当成时区符号（split("-") 会把年月日也拆掉）。
    s = str(ts).replace("T", " ").replace("Z", "").strip()
    s = re.sub(r"[+-]\d{2}:?\d{2}$", "", s).split(".")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            # GitHub 的时间戳是 **UTC**（形如 2026-09-01T12:00:00Z）。
            # time.mktime 按**本地时区**解析，UTC+8 下会偏大约 8 小时，
            # 让 --prune 的 3 天宽限期提前约 1/3 天触发；东八区之外同样有偏。
            # calendar.timegm 才是按 UTC 解析，与时间戳语义一致。
            return (time.time() - calendar.timegm(time.strptime(s, fmt))) / 86400.0
        except (ValueError, OverflowError):
            continue
    return None


def _branch_date(binfo):
    """分支最后一次提交时间（孤儿分支没有 PR，只能靠它判断活动）。"""
    c = (binfo or {}).get("commit") or {}
    inner = c.get("commit") or {}
    return ((inner.get("committer") or {}).get("date")
            or (inner.get("author") or {}).get("date"))


def ref_sha(branch):
    """取分支 head；分支不存在返回 None —— 404 在这里是正常查询结果。

    必须走 _ref_path() 编码，不能裸拼 f"/git/refs/heads/{branch}"：
    分支名含 `?` 时其后的部分会被当成 query string 丢掉，请求打到**另一个
    分支**上（实测：refs 里有 task/a 与 task/a?b 时，查后者返回前者的 sha）。

    这个错误比 404 危险得多 —— 它不报错，只是静默返回一个看似合理的值，
    而下游四处都拿它当事实：
      · 撞名检测（新分支命名循环）→ 误判「已存在」，连试 5 次后中止推送
      · --hold 复用分支             → bhead 取到别的分支的 sha
      · abandon_task 内容校验       → 该拦的「分支上有他人改动」拦不住
      · _cleanup_failed_branch      → 判成「已被他人改动」而跳过清理，孤儿分支复发
    """
    r = api("GET", _ref_path(branch), allow_404=True)
    return (r or {}).get("object", {}).get("sha")


def _pr_index(state="all"):
    """一次拉全量 PR，返回 {head_ref: PR} 索引。

    --prune 原来对每个分支调一次 find_pr()：150 个分支 = 150 次请求。
    而 find_pr 内部是一次 /pulls?...&head=owner:branch 查询，
    完全可以用**一次全量拉取 + 内存索引**替代。

    同一 head 分支可能对应多个 PR（关闭后重开），保留**最新**的那个，
    与 find_pr 的语义一致。
    """
    # N414（第一百三十三轮，中危）：docstring 原本断言「与 find_pr 的
    # 语义一致」，实测**两处都不一致**，而且是双向的：
    #
    # ① open 不优先 —— _pr_index 只比 number。分支 wip 上同时有
    #    开启 PR #10 与已合并 PR #20 时：
    #      --prune      → #20（已合并 265 天）→ 报「应当清理」+ 照抄命令
    #      delete_branch→ find_pr → #10（open）→「删除会丢改动！」
    #    **结论相反**。而这正是 prune 自己承诺要守的那一条（docstring：
    #    「[保留] PR 仍开启 —— 可能是进行中的工作，不删」）。
    #
    # ② 不校验 head 仓库 —— 只按 head.ref 建键。本仓库分支 patch-1
    #    没有任何本仓库 PR，但远端存在一个来自 **fork** 的 PR #30、
    #    head.ref 恰好也叫 patch-1（已合并）→ prune 据此建议删除
    #    patch-1，而执行侧 find_pr 带 `head=OWNER:branch` 过滤拿不到它
    #    →「没有任何 PR（孤儿分支，删除会丢改动！）」。
    #    **一个与本仓库分支毫无关系的 fork PR，成了删除依据。**
    #
    # 修法不是"照抄一份 find_pr 的逻辑"（那又会漂移），而是两者共用
    # `_pick_pr()`；head 仓库校验放在建索引时（find_pr 由服务端过滤）。
    cand = {}
    for pr in _paginate(f"/pulls?state={state}") or []:
        head = pr.get("head") or {}
        ref = head.get("ref")
        if not ref:
            continue
        # ② 只认**本仓库**作为 head 的 PR。fork 的同名分支 PR 不算。
        # （repo 字段缺失时不过滤 —— 老式响应/精简对象不猜着排除。）
        _hrepo = (head.get("repo") or {}).get("full_name")
        if _hrepo and _hrepo.lower() != f"{OWNER}/{REPO}".lower():
            continue
        cand.setdefault(ref, []).append(pr)
    return {ref: _pick_pr(v, state) for ref, v in cand.items()}


def _pick_pr(matches, pr_state="all"):
    """从同一 head 分支的多个 PR 里挑「代表」——**唯一**的口径。

    --prune 的 `_pr_index()` 与 --delete-branch 的 `find_pr()` 必须共用
    这一份（N414）：两者不一致时，体检报告说「应当清理」并给出可照抄
    的删除命令，执行侧却说「删除会丢改动」—— 同一份事实两个结论。
    「修一处 ≠ 修全部」在这个族里反复出现，抽成一份才是解法。
    """
    if not matches:
        return None
    if pr_state != "all":
        # 调用方已指定状态，按调用方要求返回（保持原语义）
        return matches[0]
    for p in matches:
        if p.get("state") == "open":
            return p
    # 不依赖返回顺序：显式按 number 取最大。
    return max(matches, key=lambda p: p.get("number") or 0)


def find_pr(branch, pr_state="all"):
    """找 head 为该分支的 PR。分支名含 /，进 query 必须转义。

    state=all 时**开启中的优先**，不能返回第一个匹配。
    一个分支可能被反复用过（同名分支关掉 PR 后再开），GitHub 会保留
    历史 PR。返回第一个的话，很可能拿到一个**早已合并/关闭**的旧 PR，
    于是调用方看到 merged_at 为空就报「未合并，删除会丢改动」——
    方向恰好反了：改动其实早就在主干里了。

    取最新的：GitHub 默认按创建时间**降序**返回（实测取回 25 条，
    number 序列是 25,24,…,1 —— 降序）。
    原先注释写"升序"，于是 `matches[-1]` 取到的是**最旧**的一条，
    与 docstring 声称的"取最新"相反。而同一分支上若同时有
    #10（已合并）与 #20（开启中），会拿到 #10 并据此判"已合并"。

    不依赖返回顺序：显式按 number 取最大。这样即便 GitHub 改了默认
    排序也不会出错 —— 依赖对方未承诺的顺序本身就是隐患。
    """
    prs = _paginate(f"/pulls?state={pr_state}"
                    f"&head={OWNER}:{quote(branch, safe='')}")
    matches = [p for p in prs or []
               if p.get("head", {}).get("ref") == branch]
    # N414：口径收敛到 `_pick_pr()`，与 _pr_index 共用同一份 ——
    # 不再"各自写一份、靠注释声明一致"。
    return _pick_pr(matches, pr_state)


def new_branch_name():
    """每次推送都新建分支：基点永远是当前主干，PR diff 才干净。

    随机后缀防并发撞名（同一秒内两人各推一次会撞纯时间戳）。
    """
    # 4 字节 = 8 hex ≈ 43 亿种。原来是 2 字节（4 hex = 65536 种），
    # 多人同时推或 CI 并发任务时，同一秒内撞名的概率不可忽略；
    # 撞名后 create_branch 收到 422，整个推送白跑一趟。
    return TASK_PREFIX + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(4).hex()


def create_branch(branch, main_head):
    try:
        r = api("POST", "/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": main_head})
    except SystemExit as e:
        # 422「Reference already exists」的真实成因是**分支名撞车**
        # （另一台机器同一秒生成了同名分支），与「远端已前进」毫无关系。
        # 走通用 hint 会让用户去 --pull，排查方向完全跑偏（P2-03）。
        if "422" in str(e) and "already exists" in str(e).lower():
            raise SystemExit(
                f"创建分支 {branch} 失败：该分支已存在。\n"
                f"  多半是另一台机器/另一个进程同时生成了同名分支。\n"
                f"  重跑一次即可（分支名带随机后缀，撞名概率极低）。")
        raise
    s = r.get("object", {}).get("sha")
    if not s:
        raise SystemExit(f"创建分支 {branch} 失败: {json.dumps(r)[:300]}")
    return s


def ensure_pr(branch, msg):
    """开 PR。分支是新建的，所以通常是新 PR（--hold 复用分支时才可能命中旧的）。

    **只复用 state == "open" 的 PR**。
    曾经不检查 state，直接命中 find_pr 的第一个结果。find_pr 默认查
    state=all，于是已关闭 / 已合并的旧 PR 会被复用：
      · 已合并：GitHub 对再 PUT /merge 返回 405 not mergeable，
        被 _NEED_UPDATE_HINTS 误判成「分支不是最新的」，提示完全跑偏
      · 已关闭：合并必然失败
    两种情况的后果都是「推送成功、改动不进主干、报错误导」，
    而用户只是用了同一个 -b 分支名（脚本自己也常提示带 -b）。
    """
    pr = find_pr(branch)
    if pr and pr.get("state") == "open":
        return pr, False
    pr = api("POST", "/pulls", {
        "title": (msg.splitlines()[0][:100] if msg else f"更新 {branch}"),
        "head": branch, "base": BRANCH,
        "body": "由 push_api.py 自动创建",
    })
    if "number" not in pr:
        raise SystemExit(f"创建 PR 失败: {json.dumps(pr)[:300]}")
    return pr, True


# GitHub 拒绝合并时的两类原因，处理方式完全不同：
#   conflict   —— 内容真冲突，要人解决
#   need_update —— 分支不够新（仓库开了「合并前必须同步主干」保护），
#                  重新基于最新主干推一次即可，改动本身没冲突
_NEED_UPDATE_HINTS = (
    "Base branch was modified",     # 主干在 PR 打开后前进过
    "not up to date", "up-to-date",
    "not mergeable",
)

# CI 状态**必须独立成类**，不能混进 _NEED_UPDATE_HINTS。
#
# 混在一起的后果：CI 未跑完被判成「分支不是最新的」→ 提示去点
# 「Update branch」—— 照做完全无效，因为分支本来就是最新的，
# 卡住的是 CI。用户会以为工具坏了，进而去试 --force-overwrite。
#
# 本脚本不主动轮询 check-runs（那需要额外 API 与等待策略），
# 但至少要**如实归类**，给出能真正推进的指引。
# N436：405「合并方式不被允许」的判定词（必须是这一类才改判）
_MERGE_METHOD_DENIED_HINTS = (
    "is not allowed",
    "are not allowed",
    "not allowed for this repository",
    "merge method",
)

_CI_PENDING_HINTS = (
    "Required status check",
    "required status checks",
    "status check",
    "checks have not",
)


def _pr_mergeability(number, tries=4, delay=1.0):
    """取 PR 的权威合并状态 (mergeable, mergeable_state)。

    GitHub 的 `mergeable` 是**异步计算**的：刚创建的 PR 首次查询会返回
    `None`（表示「还没算出来」，不是「不能合并」）。必须轮询等它出结果，
    否则 `None` 会被当成 falsy → 一律判成 conflict，把「分支不够新」
    也误判成内容冲突。

    mergeable_state 的取值（GitHub 文档）：
      clean    —— 可合并
      dirty    —— 内容冲突（要人解决）
      behind   —— 分支落后于主干（仓库开了「必须同步」保护）
      blocked  —— 被保护规则挡住（需 review / 状态检查未过）
      unstable / draft / unknown
    """
    mergeable, state_name = None, None
    for i in range(tries):
        pr = api("GET", f"/pulls/{number}")
        if not isinstance(pr, dict):
            break
        mergeable = pr.get("mergeable")
        state_name = pr.get("mergeable_state")
        if mergeable is not None:
            break
        # mergeable 还没算出来：触发一次计算并等待
        if i + 1 < tries:
            time.sleep(delay * (2 ** i))     # 1s, 2s, 4s
    return mergeable, state_name


def merge_pr(number, method="squash"):
    """合并 PR。返回 (结果, 状态)；被拒时结果是 None。

    GitHub 用 409/405 同时表示「内容冲突」和「分支不够新」，
    不区分的话会把后者当冲突处理，引导人去做根本不需要的冲突解决。

    判据优先级：
      1. **权威字段** mergeable / mergeable_state（准）
      2. 拿不到时退回错误信息匹配（兜底，不如前者可靠）
    """
    try:
        r = api("PUT", f"/pulls/{number}/merge", {"merge_method": method})
    except SystemExit as e:
        msg = str(e)
        if not ("HTTP 409" in msg or "HTTP 405" in msg):
            raise

        # ① 先问 GitHub 自己：这个 PR 到底能不能合、为什么不能
        try:
            mergeable, mstate = _pr_mergeability(number)
        except BaseException:
            mergeable, mstate = None, None     # 查不到就退回字符串匹配

        # N438（第一百四十二轮，低危）：`mergeable_state` 的 "unknown" 与
        # None 表达的是**同一件事**（GitHub 还没算出来 / 不给状态），
        # 而 `if mstate:` 对前者为真、对后者为假 → 同一个事实落到两条
        # 不同的兜底路径，**结论相反**。实测（唯一变量是 mstate）：
        #   mergeable=False + 消息 "Pull Request is not mergeable"
        #     unknown → need_update（走兜底，命中 _NEED_UPDATE_HINTS）
        #     None    → conflict  （走 `mergeable is False and not mstate`）
        # 两条给出的指引方向相反：一条说"去点 Update branch"，另一条说
        # "去解决冲突" —— 而真实情况是 GitHub 还没算出状态，两者都不成立。
        # "unknown" 是 GitHub 文档列出的正常取值（大仓库 / 异步未完成），
        # 不是理论分支。归一化：unknown 视同没有状态。
        if mstate and str(mstate).lower() == "unknown":
            mstate = None

        if mstate:
            s = mstate.lower()
            if s == "dirty":
                return None, "conflict"
            # N435（第一百四十二轮，中危）：`blocked` 在 GitHub 权威定义里
            # 就是「被保护规则挡住（需 review / 状态检查未过）」——
            # 也就是说 **CI 没跑完时 mergeable_state 恰恰就是 blocked**。
            # 而这里直接 return need_update，排在下面 _CI_PENDING_HINTS
            # 之前 → ci_pending 那一段**永远走不到**（实测同一条错误消息、
            # 唯一变量是 mstate：blocked→need_update，None/clean→ci_pending）。
            #
            # 于是真实场景下 CI 未跑完被判成「分支不是最新的」，
            # 提示去点 Update branch —— 照做完全无效，分支本来就是最新的。
            # 先把 CI 类挑出来，再落到 blocked 的通用判定。
            if s == "blocked" and any(
                    h.lower() in msg.lower() for h in _CI_PENDING_HINTS):
                return None, "ci_pending"
            if s in ("behind", "blocked", "unstable", "draft"):
                # behind  = 分支不够新（重新基于最新主干推一次即可）
                # blocked = 保护规则挡住（需 review / 状态检查）
                # 这两类都不是内容冲突，不该引导人去手工解冲突
                return None, "need_update"
        if mergeable is False and not mstate:
            return None, "conflict"

        # N436（第一百四十二轮，中危）：405 承载**第三类**语义 ——
        # 「合并方法不允许」，被并入「内容冲突」。实测：
        #   405 + mergeable=True + state="clean" + "Squash commits is not allowed"
        #   → conflict   ★
        # 而 `_pr_mergeability` 明确返回 (True, "clean")：GitHub 说这个 PR
        # **可以合并、没有冲突**。脚本拿到权威答复却返回 conflict。
        #
        # 可达性不是边缘：`--method` 的 CLI 默认值是 squash，而仓库设置里
        # 可以只开 merge commit 关掉 squash —— 这种仓库上**默认路径必然 405**。
        # 引导人去做根本不需要的冲突解决，还会诱发 --force-overwrite。
        # 只在**确属**"合并方式不被允许"时才改判。405 同时也承载
        # 「必需状态检查未过」（实测：HTTP 405 + "Required status check
        # … is failing"）—— 那属于 CI 类，改判会把它从 ci_pending 抢走，
        # 又退回"把人推向无效操作"的老问题。
        _low0 = msg.lower()
        if "HTTP 405" in msg and any(
                h in _low0 for h in _MERGE_METHOD_DENIED_HINTS):
            return None, "method_not_allowed"

        # ② 兜底：错误文案匹配
        low = msg.lower()
        # CI 类必须**先于** need_update 判定。
        #
        # 原文把 "Required status check" 放在 _NEED_UPDATE_HINTS 里，
        # 于是 CI 未跑完被判成「分支不是最新的」→ 提示去点「Update branch」
        # —— 照做完全无效：分支本来就是最新的，卡住的是 CI。
        # 用户会以为工具坏了，进而尝试 --force-overwrite。
        if any(h.lower() in low for h in _CI_PENDING_HINTS):
            return None, "ci_pending"
        if any(h.lower() in low for h in _NEED_UPDATE_HINTS):
            return None, "need_update"
        return None, "conflict"
    if not r.get("merged"):
        # N437（第一百四十二轮，中危）：PUT /merge 返回 2xx 但 `merged ≠ true`
        # → 假成功。实测四种 2xx：
        #   200 merged=true                        → merged
        #   200 空 body（api 解析成 {}）             → 仍报 ✅ 已合并  ★
        #   200 merged=false + "not mergeable yet"  → 仍报 ✅ 已合并  ★
        #   200 无 merged 字段 {"sha":..}            → 仍报 ✅ 已合并  ★
        # 第三行响应体原文就是 "not mergeable yet"。
        #
        # 唯一调用方 `do_merge` 用 `if r is None` 判失败，于是只要 r 不是
        # None 就按"已合并"处理 —— 接着删分支、写审计"merge_ok"。
        # 空 body 是脚本自己允许的（`_json_or_die` 显式 `body or "{}"`），
        # 但没有随之考虑它对"合并是否成功"的语义影响。
        #
        # 返回 None 而不是 r：被拒时结果是 None，这个契约不能破。
        return None, ("not_merged:" + (r.get("message") or "unknown"))
    return r, "merged"


def delete_branch(branch):
    api("DELETE", _ref_path(branch), allow_404=True)


def _compare(base_ref, head_ref):
    """compare 两个 ref。返回 dict；拿不到返回 {}（由调用方按「无法确定」处理）。

    注意路径里的 / 语义与 _ref_path() **相反**：
      · _ref_path()  端点是 /git/refs/heads/{ref}，heads/ 之后整段都是 ref 名
                     → 必须 quote(ref, safe="/") 保留分支名里的斜杠
      · /compare/{base}...{head} 里的 / 是**路径分隔符**
                     → 分支名 task/2026-xxx 的斜杠必须编成 %2F，即 safe=""
    直接复用 _ref_path 会把 compare 请求打到不存在的路径上（404），
    而 404 在这里会被当成「没依据」→ 一律 fail-closed 拦下，表现为
    --merge 莫名被拒且说不清原因。
    """
    path = "/compare/{}...{}".format(quote(base_ref, safe=""),
                                     quote(head_ref, safe=""))
    try:
        return api("GET", path, allow_404=True) or {}
    except SystemExit:
        # 比较失败（422 diff 过大 / 网络）不等于「有风险」，但也**不能**
        # 据此断言「没风险」—— 如实回空，交给上层 fail-closed。
        return {}


def _their_files_by_tree(base_sha, head_sha):
    """主干在基点之后改了哪些文件。返回 (集合, 依据是否可靠, 原因)。

    第三项「原因」是 N430 的依据来源（N431：原 docstring 只写了二元组，
    等于告诉读代码的人"没有原因可用"，会阻止后来者补上 N430）。

    为什么改用 tree 比对而不是 /compare：

    /compare 的 files 数组上限 GitHub **未公开保证**（实测约 300），
    于是只能用 COMPARE_FILE_CAP=250 去「猜」有没有被截断 —— 到达阈值
    就当「可能残缺」处理。这是把安全判据押在一个观测值上。

    而 /git/trees 返回的是 **git 自己的数据结构**，带官方的 truncated
    标志：截断与否由服务端明说，不用猜。同样一个判断，一个靠猜阈值，
    一个靠权威标志 —— 该用后者。

    额外两个好处：
      · remote_state_map 按 commit sha 缓存（树是内容寻址的，不可能变），
        主流程多半已拉过 head 的树，这次直接命中缓存、零请求
      · 不需要 diff 内容，只比 (mode, sha)，与 git 的判断口径一致

    判据：新增 + (mode,sha) 变了 + 删除。删除也要算 ——
    他删掉的文件我可能还在引用。

    base_sha 通常是历史 commit（基点），其树**永不变**，缓存命中率极高。
    """
    try:
        base_map = remote_state_map(base_sha)
        head_map = remote_state_map(head_sha)
    except GuardBlock:
        # N429（第一百四十轮，中危）：M75 把「tree 被截断」从裸 SystemExit
        # 改成 `_blocked()`（GuardBlock，退出码 2），理由是「判不了所以不推，
        # 改动一个没丢 —— 是拦下，不是错误」。
        #
        # 但 GuardBlock 是 SystemExit 的**子类**（那条注释还把它写成优点：
        # "能被现有 except SystemExit 正常接住"）—— 于是下面那个
        # `except SystemExit` **照旧把它吞掉**，转成 (set(), False, why)。
        # 上层按 unknown → do_merge 返回假 → 而 _BLOCKED_BY_GUARD 从未
        # 被置 → 顶层判据读到 None → **exit 0**。
        #
        # 实测（同一份 mock，唯一变量是调用路径）：
        #   [A] 直接调 remote_state_map  → GuardBlock code=2 → exit 2
        #   [B] 经本函数                → (set(), False, why) → exit 0
        # M75 设计的两个退出码（截断该 2、取树失败该 1）在这条路径上
        # **都被抹成 0**。而 0 是 CI 判"成功"，比 M75 担心的两种
        #（噪音告警 / 为压噪音漏报）都更难发现 —— 主干一个字节没变。
        #
        # 处置：GuardBlock 是「拦下」语义，**不吞**，原样上抛交回顶层
        # fail-closed。只有普通 SystemExit（请求没做成）才转成
        # "依据不可靠" —— 那是 M75 明确要求**保持**的语义。
        #
        # ⚠ 必须排在 `except SystemExit` **之前**：GuardBlock 是它的子类，
        # 顺序反了永远进不来（实测）。
        raise
    except SystemExit as e:
        # remote_state_map 取树失败（网络/限流/404）会抛普通 SystemExit。
        # 那是「这条依据也拿不到」，不是错误 —— 如实回不可靠，
        # 交给上层 fail-closed，不能当成「没改动」。
        return set(), False, str(e)[:120]
    except (OSError, ValueError) as e:
        # N431：原来这里是裸 `except Exception`，会把**编程错误**
        #（AttributeError / KeyError / TypeError）一并吞成"依据不可靠"
        # → unknown → exit 0。一个 bug 于是被伪装成"保守拦下"，
        # 与 N429 同一后果但更难发现（连 why 里都没有真实类型）。
        # 收窄到真实的运行环境异常类。
        return set(), False, f"{type(e).__name__}: {e}"[:120]

    changed = set()
    for path, val in head_map.items():
        if base_map.get(path) != val:
            changed.add(path)
    for path in base_map:
        if path not in head_map:
            changed.add(path)                # 被删除
    return changed, True, None


def _merge_preflight(state, branch):
    """合并前的基点校验（第四层）。返回 (verdict, info)。

    verdict 四值：
      ok       分支基点就是主干最新（merge base == 主干 head）  直接合，不打招呼
      clean    主干前进过，但改的文件与我推的无交集            合，打印一行说明
      overlap  主干前进且动了我推的文件                        拦下，保留分支和 PR
      unknown  拿不到完整依据（compare 无响应 / 列表被截断）    拦下，fail-closed

    为什么要这一层：现有三层防护（P0-1 副本过期 / 文件级基线 / ref 乐观锁）
    **全部在推送之前**，而合并发生在推送之后 —— 建 blob、建 tree 那几十秒里，
    别人完全可能已经合并了别的 PR（本机的 flock 只管同机并发，管不到别人的
    机器）。主干一前进，PR 的 merge base 就不再是「你校验过的那个主干」，
    而你本地也没有那些改动。

    判据选「文件交集」而不是「分支是否最新」：
      · 「不够新就一律拦」太粗 —— 主干改了 README.md 而你改的是 a.js，
        拦下只会把人推向 --force-overwrite
      · 给全员加串行锁需要跨机器协调设施，脚本不该有，那会牺牲所有人的
        推送速度去防一个偶发场景
      · 文件交集精确命中真正危险的那一种：两边动了同一个文件
    """
    head = ref_sha(BRANCH)
    info = {"branch": branch, "base": None, "head": head,
            "mine": [], "theirs": [], "hit": [], "why": None,
            "mine_truncated": False, "theirs_truncated": False,
            # N616（第二百〇三轮，低危）：mine 的**来源**必须单独记一维。
            # 上面那句注释承诺「退回分支 diff 时打上标记、那份依据更弱」，
            # 而 `mine_truncated` 表示的是「列表不全」（not _rel2）——
            # 兜底且 tree 比对成功时它仍然是 False，于是「弱依据（分支
            # diff 推出来的）」与「强依据（本地记录）」在 info 里**完全
            # 不可区分**：下游与 `--json` 消费者都看不出这一份更弱。
            "mine_source": None}
    if not head:
        return "unknown", info

    # 第一次 compare 同时给出 merge base 与「我推了哪些文件」的兜底来源。
    # 单人无并发时这一步就发现 mb == head，直接 ok —— 只多这一次请求。
    cmp = _compare(BRANCH, branch)
    mb = (cmp.get("merge_base_commit") or {}).get("sha")
    info["base"] = mb
    if not mb:
        return "unknown", info
    if mb == head:
        return "ok", info

    # theirs 走 tree 比对（官方 truncated 标志），不用 /compare 的 250 猜测。
    # 失败时 theirs_trunc=True 由上游按 unknown 处理（fail-closed）。
    theirs, _reliable, _why = _their_files_by_tree(mb, head)
    theirs_trunc = not _reliable
    if not _reliable:
        info["why"] = _why

    # mine 优先取本地记录：精确，且不依赖 diff 完整性。
    # 状态文件丢了（换机器 / 重建基线）才退回分支 diff —— 那份依据更弱，
    # 不能与本地记录同等对待。
    #
    # N616：弱与强写进 info["mine_source"]，不再只靠 mine_truncated
    # （它只覆盖「列表不全」这一格，兜底且比对成功时是 False，
    #  于是"依据更弱"这件事当时没有任何字段承载）。
    pushed = ((state.get("tasks") or {}).get(branch) or {}).get("pushed")
    mine_trunc = False
    if isinstance(pushed, dict) and pushed:
        mine = set(pushed.keys())
        info["mine_source"] = "local_record"
    elif isinstance(pushed, list) and pushed:
        mine = set(pushed)
        info["mine_source"] = "local_record"
    else:
        # 退回分支 diff 时也走 tree 比对，且用**三点语义**：
        # compare(mb, 分支) 才是「我这条分支改了什么」，
        # 而 compare(主干, 分支) 是两点语义，会把主干新增的文件也算进来
        # —— 那些不是我推的，算进来只会误判 overlap。
        # （原实现走 /compare 取 files 数组，语义就是错的；那段封装
        #  N230 已删——它改走 tree 比对后就是零调用点的死函数，
        #  docstring 却完整描述了三点语义，看着像在服役。）
        bsha = ref_sha(branch)
        if bsha:
            mine, _rel2, _why2 = _their_files_by_tree(mb, bsha)
            mine_trunc = not _rel2
            info["mine_source"] = "branch_diff"
            if not _rel2:
                info["why"] = _why2 or info.get("why")
        else:
            mine, mine_trunc = set(), True
            info["mine_source"] = "none"

    hit = sorted(mine & theirs)
    info.update(mine=sorted(mine), theirs=sorted(theirs), hit=hit,
                mine_truncated=mine_trunc, theirs_truncated=theirs_trunc)
    # 交集非空就是非空：列表被截断也照判 overlap（截断只会漏报，
    # 不会把「有」判成「无」）。
    if hit:
        return "overlap", info
    # 交集为空但依据不全 → unknown，不能当「没交集」放过。
    if mine_trunc or theirs_trunc:
        return "unknown", info
    return "clean", info


def _report_merge_preflight(verdict, info, branch, number):
    """把第四层的结论说成能照做的下一步。纯输出，不做决定。"""
    if verdict == "clean":
        print(f"\n  · 主干在你建分支后前进过（{str(info.get('base'))[:8]} → "
              f"{str(info.get('head'))[:8]}），但没有动你推的文件，直接合并。")
        return
    if verdict == "overlap":
        hit = info.get("hit") or []
        shown = "、".join(hit[:5]) + ("…" if len(hit) > 5 else "")
        audit({"kind": "merge_block", "pr": number, "branch": branch,
               "verdict": "overlap",
               # N140：键名写成了 `overlap`，而 _merge_preflight 的 info
                # 里根本没有这个键（交集字段叫 `hit`）→ 恒为 None。
               # 审计里最关键的一项（"哪些文件同时被两边改动"）永远空着，
               # 事后复盘无从下手。
               "files": (info or {}).get("hit") or []})
        print(f"\n❌ PR #{number} 暂不合并：主干在你建分支后又改了你推的文件。")
        print(f"   同时被两边改动：{shown}")
        print("\n   git 的三方合并只保证文本不冲突，不保证语义正确：")
        print("   他删掉的函数、你可能还在调；两边自动合上后代码不一定能跑，")
        print("   而这个组合**从未在你或他的机器上存在过**，合并完也不会报错。")
        print("\n   正确顺序（把组合拉回本地，验证过再合）：")
        print("     python3 push_api.py --pull        # 主干改动合进本地")
        print("     # 本地跑一遍确认没问题")
        print("     python3 push_api.py               # 用新基点重建分支再推")
        print(f"\n   分支 {branch} 与 PR #{number} 已保留，改动没丢。")
        print("   确认无需验证时：加 --allow-stale-merge 直接合并。")
        return
    # unknown：必须说清是「没查到」而不是「查到有风险」——
    # 否则用户会去排查一个并不存在的冲突。
    # N430（第一百四十轮，中危）：这条归因指向一个**已被废弃且不存在**的机制。
    #
    # COMPARE_FILE_CAP=250 是 `/compare` 的 files 数组上限（一个观测值猜测
    # 机制），而 theirs **早就改用 tree 比对**了 —— 本函数的 docstring 用
    # 整段论证过这件事（/git/trees 带官方 truncated 标志，不用猜）。
    # **改造换了数据源，没换报错文案。**
    #
    # 实测三种形态、两种归因全错：
    #   tree 截断 → 真实原因「远端 tree 被截断（仓库过大）」
    #             → 用户看到「改动列表可能超过 250 个文件上限」
    #   403 限流 → 真实原因「HTTP 403: API rate limit exceeded」
    #             → 用户看到同一句 250 的文案
    # **403 限流被报成"改动列表超过 250 个文件"**，用户会去查一个
    # 并不存在的方向。
    #
    # 真实原因就在同一个 dict 里（`_merge_preflight` 已写进 info["why"]），
    # 只是这里一次都没读。优先取它，取不到才退回原来的描述。
    why = (info.get("why")
           or ("分支改动列表不完整（依据不全，无法确认完整清单）"
               if info.get("theirs_truncated") or info.get("mine_truncated")
               else "未能取得完整的主干改动列表"))
    audit({"kind": "merge_block", "pr": number, "branch": branch,
           "verdict": "unknown", "why": str(why)[:120]})
    print(f"\n❌ PR #{number} 暂不合并：无法确定主干有没有动你推的文件（{why}）。")
    print("   判断「没交集」依赖改动列表完整；列表不全时按「可能有交集」处理，")
    print("   因为漏报的代价（未验证的组合被静默合并）远大于多等一次。")
    print("\n   二选一：")
    print("     python3 push_api.py --pull        # 拉回本地，用新基点重建分支再推")
    print("     python3 push_api.py --merge --allow-stale-merge   # 直接合并")
    print(f"\n   分支 {branch} 与 PR #{number} 已保留，改动没丢。")


def _paginate(path, cap=50):
    """分页拉取。last_truncated 记录本次结果是否被截断（N70）。

    拉一个列表接口的**全部**页。

    三处列表接口原来都是 `per_page=100` 取第一页、不翻页。
    分支或 PR 超过 100 条时：
      · --prune 静默漏分支 ——「分支体检」报「很干净」，实为漏报；
        这类**看起来正常的错误安全感**比崩溃更危险
      · --merge 的候选列表漏 PR，用户以为没有可合并的
      · find_pr 漏 PR，可能复用到一个陈旧的

    cap 是页数上限（默认 50 页 = 5000 条），防止接口异常时无限循环。
    触顶时打印警告，不假装结果完整。
    """
    # N163 的第二半：每次调用**开头**先复位。
    # 原实现只置 True 不置 False（复位清单补的是另一处），于是标志
    # 只增不减 —— 一旦某个列表触顶，之后**所有**列表读取都拿到 True。
    _paginate.last_truncated = False
    out, page, sep = [], 1, "&" if "?" in path else "?"
    while page <= cap:
        batch = api("GET", f"{path}{sep}per_page=100&page={page}") or []
        if not isinstance(batch, list):
            break
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1
    print(f"  ! 列表超过 {cap * 100} 条，已截断：{path}")
    # N70：把截断状态暴露出来。下游若有"不在列表里即不存在"的判据
    # （如 _prune_stale_tasks），必须据此停止清理 —— 否则会删掉
    # 真实存在但没被列出的条目。
    _paginate.last_truncated = True
    return out


def _paginate_ex(path, cap=50):
    """N302：返回 (out, truncated) —— truncated 是**本次这个列表**的。

    为什么要有这个：prune() 在读截断标志之前会调**两次**分页
    （`list_branches()` 与 PR 列表），而标志是全局单处
    （`_paginate.last_truncated`）。PR 触顶就会把它置成 True，
    随后被当作"分支列表被截断"使用。

    真实复现（函数级）：
        分支列表拉完 → last_truncated = False
        PR 列表触顶   → last_truncated = True
        ★ prune 读到 True（正确应为 False，分支列表是完整的）

    PR 数远超分支数是常态（分支会被合并后删除，PR 只增不减），
    所以这不是边缘场景。两条后果：

      1. 清理**静默失效** —— _prune_stale_tasks 收到 True 直接 return，
         tasks 里的僵尸条目永远清不掉。
      2. 文案是**假的** —— 打印"远端分支列表可能被截断"，
         而实际截断的是 PR 列表；用户会去查分支数量，排查方向全错。
    """
    _paginate.last_truncated = False
    out = _paginate(path, cap=cap)
    return out, bool(getattr(_paginate, "last_truncated", False))


def _list_branches_ex():
    """返回 (branches, truncated)：truncated 只反映**分支列表**本身。"""
    return _paginate_ex("/branches")


def list_branches():
    return _paginate("/branches")


def _refresh_main_baseline(state, new_head, merged_pushed=None,
                           refreshed_out=None):
    """主干前进后刷新基线：只更新「本地已是主干版本」的文件。

    本地还没合上的**保留旧基线** —— 否则 --pull 会认为主干没变过，
    再也拉不下来（和推送后刷新基线同一个坑）。

    merged_pushed：本次 PR 推到分支的内容 {path: sha}。
    合并成功后，主干上这些文件的版本就是（或包含）**我推的内容**，
    因此可以无条件刷新基线 —— 否则会出现这种误判：
        --hold 推 v2 → 继续编辑成 v3 → --merge（主干 = v2）
        本地 v3 ≠ 主干 v2，按「本地落后」处理 → 提示 --pull
        而 --pull 又拿陈旧的 v1 当 base，把 v2→v3 自己的连续改动判成冲突
    实际上本地是**领先**主干，不是落后。
    """
    new_rstate = remote_state_map(new_head)
    lmap = local_state_map()
    lagging = []

    # N34：这是基线刷新点之一（原先注释写"第 5 个"，与 `_baseline_stale`
    # docstring 那句写死的计数各数各的，实际是六处 —— 见 N583）。
    # 此处此前是唯一**不**同步 local_seen 的。
    # 后果：PR 工作流下合并成功后基线已前进，而「本地曾经有过」清单没跟上
    # —— 本次新增/合并进来的路径不在清单里，日后它们被删时会因
    # 「判据 3：本地从未拥有过」被判成远端独有而漏删（实测静默）。
    # PR 工作流是默认路径，这个缺口影响面最大。
    if _sync_local_seen:
        try:
            _sync_local_seen(state, lmap)
        except Exception as _e:
            # 告知性字段，同步失败不该阻断基线刷新
            print(f"  ! local_seen 同步失败：{type(_e).__name__}: {_e}")

    # -------- 摘掉「合并后才真正生效」的删除（N2）--------
    # PR 工作流下，删除在建 tree 时只登记进 pending_removals，此时主干
    # **还没删**（要等 merge）。现在主干真的前进了，才可以把它们从基线
    # 里摘掉。提前摘会让基线谎报「主干上没有」，等 PR 期间跑 --pull
    # 会把文件当「远端新增」写回本地 —— 用户刚删的东西回来了。
    #
    # 只对**确实已从主干消失**的路径摘除：合并失败 / 只合了部分时，
    # 仍在主干上的文件必须留在基线里，否则会漏掉后续的真删除。
    for _p in list(state.get("pending_removals") or []):
        if _p not in new_rstate:
            state["files"].pop(_p, None)
    if state.get("pending_removals"):
        _still = [q for q in state["pending_removals"] if q in new_rstate]
        state["pending_removals"] = _still
        if not _still:
            state.pop("pending_removals", None)

    for p, (m, s) in new_rstate.items():
        if lmap.get(p) == (m, s):
            state["files"][p] = {"mode": m, "sha": s}
            # N571（A151，中危）：这一档「本地已是远端版本 → 基线跟进」
            # 此前**只写基线、不上报**，于是 direct 侧有那句「N 个非本次
            # 推送的文件远端已更新…基线已跟进」的告知，PR 侧一次都没有。
            #
            # 同一份 state、同一个远端、同一个本地，唯一变量是工作流
            # （`DEFAULT_WORKFLOW = "pr"`），而 PR 是默认路径 —— 即默认
            # 情况下这句告知**永远不出现**。用 out 参数而不是改返回值：
            # 返回值 `lagging` 是既有契约，改它会波及所有调用点。
            if refreshed_out is not None:
                refreshed_out.append(p)
            continue
        if merged_pushed and merged_pushed.get(p) == s:
            # 主干上这个版本正是我推上去的 → 基线跟进，本地的后续改动
            # 就落在正确的 base 上，不会再被当成「落后」或假冲突。
            state["files"][p] = {"mode": m, "sha": s}
            continue
        if p not in lmap:
            # 本地根本没有这个文件 → 是**主干新增**，不是「本地落后」。
            #
            # 原来两者都进 lagging，于是 synced_commit 被置 None，
            # 结果是「刚合并成功 → 立刻被 P0-1 拦住」—— 用户刚推送完
            # 就被挡在门外，且提示的出路是 --pull（没什么可拉的）。
            #
            # 主干新增不会造成覆盖：用户的 todo 里不会有它（没改过），
            # 推送时不会带上；本地补齐由 --pull 的「本地缺失」分支负责
            # （那条是独立判据 p not in lmap，不依赖 lagging）。
            state["files"][p] = {"mode": m, "sha": s}
            continue
        old = state["files"].get(p) or {}
        # N630（A128，中危）：这里原先只比 sha（`old.get("sha") != s`），
        # 而「主干相对基线变过吗」这一问在本文件里还有第二份实现 ——
        # mark_synced 那一处（N422 已把它改成含 mode）。两份**互不引用**，
        # A128 实测出**已经发生**的行为分叉（不是理论风险）：
        #
        #   远端 (100755,X) 基线 (100644,X) 本地 (100644,X)  ← 只改可执行位
        #     mark_synced          → `_baseline_stale` 判 True → **拦下**
        #     _refresh_main_baseline → 只比 sha → lagging=[] →
        #       synced_commit 推进到新 HEAD，**宣布「本地已跟上主干」**
        #
        # 同一份 state、同一个远端、同一个本地，两个出口给出相反结论。
        # 而本函数正是 **PR 合并成功后**被调用的那个（`do_merge` → 刷新
        # 主干基线），所以 PR 工作流下主干被只改可执行位时，脚本一边
        # 宣布已同步，一边（`--mark-synced`）说远端改过、本地没合上。
        # 这正是本批反复记的形态：每修一处，另一处仍是旧口径。
        #
        # 改成与 `_baseline_stale` 同源 —— 它判的正是「基线条目是否还
        # 落后于 (mode, sha)」，且已处理 None / 裸 sha 字符串 / dict
        # 三种形态（裸 sha 一定会刷新，因为 v1 形态没有 mode）。
        if _baseline_stale(old, m, s):
            lagging.append(p)
    state["base_commit"] = new_head
    state["synced_commit"] = None if lagging else new_head
    return lagging


def _classify_remote_drift(state, lmap, rstate, todo):
    """把「非本次推送、但远端已经变了」的路径分成三类（**只读**，不写 state）。

    三类分别是：

      refreshed —— 本地已经是远端那个版本 → 基线可以跟进
      main_new  —— 本地根本没有这个文件（主干新增）→ 基线跟进但**不算**
                   refreshed（M165：主干新增不属「本地已是远端版本」）
      lagging   —— 本地有、但内容落后于远端 → **不**刷新基线（fail-closed）

    判据与 `_main` 直推末尾那段基线刷新、以及 `_refresh_main_baseline`
    逐字同源（`_baseline_stale` 判「基线条目是否还落后于 (mode, sha)」，
    `lmap.get(p) == (mode, sha)` 判「本地是否已是远端版本」）。

    N572（第一百八十七轮，中危）之前这段判据**只存在于 direct 分支的
    函数体内部** —— PR 模式下 `do_merge()` 成功就 `return` 了，
    `--hold`（不合并）更是连刷新都不做，两条路都拿不到分类结果，
    于是「N 个文件本地落后于远端」这句告知在**默认工作流**下一次都
    不出现。抽成函数不是为了省代码，是为了让三条出口共用**同一份**
    口径：分开写三遍必然继续漂移，而漂移的表现正是本批反复记的
    「同一份 state、两个出口给出相反结论」。
    """
    refreshed, main_new, lagging = {}, {}, []
    _todo = set(todo or ())
    for p, (rmode, rsha) in (rstate or {}).items():
        if p in _todo:
            continue
        cur = (state.get("files") or {}).get(p)
        if not _baseline_stale(cur, rmode, rsha):
            continue
        if lmap.get(p) == (rmode, rsha):
            refreshed[p] = (rmode, rsha)
        elif p not in lmap:
            main_new[p] = (rmode, rsha)
        else:
            lagging.append(p)
    return refreshed, main_new, lagging


def do_merge(state, number, method="squash", yes=False, quiet=False,
            keep_on_conflict=False, allow_stale=False):
    """合并 PR → 刷新主干基线 → 删分支。返回是否成功。

    quiet：自动合并时静音掉「确认 / 已合并」这类交互性输出。
    allow_stale：放行第四层（合并前基点校验）的 overlap / unknown 判定。
                 只放这一层，不绕过推送前的任何校验 —— 它防的是「未验证的组合」，
                 不是「丢改动」，所以放行是安全的。
    """
    pr = api("GET", f"/pulls/{number}")
    branch = pr.get("head", {}).get("ref")
    if pr.get("state") == "closed" and not pr.get("merged"):
        print(f"PR #{number} 已关闭且未合并")
        return False
    # 已合并过必须在这里短路：否则走到 PUT /merge，GitHub 回 405
    # "not mergeable"，而 "not mergeable" 就在 _NEED_UPDATE_HINTS 里 →
    # 被报成「分支不是最新的，去点 Update branch」，排查方向完全跑偏。
    # 触发场景很常见：网页上先合了，再跑 --merge。
    if pr.get("merged"):
        print(f"PR #{number} 已合并过（{pr.get('merged_at') or '时间未知'}），无需再合")
        return False

    # -------- 第四层：合并前的基点校验 --------
    # 顺序必须在下面的 "if not quiet:" 确认提示之前：
    # ok 时静默放行不该被打断，而 overlap 要在问「确认合并？」之前就拦掉 ——
    # 否则用户先回答了 y 才看到拒绝。
    verdict, info = _merge_preflight(state, branch)
    if verdict in ("overlap", "unknown"):
        if allow_stale:
            print(f"  ! --allow-stale-merge 已放行（{verdict}）："
                  f"合并结果未在你本地验证过，出问题请回看本次 PR")
        else:
            _report_merge_preflight(verdict, info, branch, number)
            return False
    elif verdict == "clean":
        _report_merge_preflight(verdict, info, branch, number)

    if not quiet:
        print(f"\n合并 PR #{number}：{branch} → {pr.get('base', {}).get('ref')}  [{method}]")
        if not yes and safe_input("确认合并？(y/N) ").strip().lower() != "y":
            _cancelled()
            return False

    r, status = merge_pr(number, method)
    if r is None and str(status).startswith("not_merged:"):
        # N437：GitHub 回了 2xx 但没说合并成功 —— 不能报 ✅ 已合并，
        # 更不能删分支（分支上还有改动）或写 merge_ok 审计。
        _why = str(status).split(":", 1)[1]
        print(f"\n❌ PR #{number} 未合并：GitHub 返回成功状态码，"
              f"但响应里没有确认合并（{_why}）。")
        print(f"   PR #{number} 与分支 {branch} **已保留**，改动没丢 —— "
              f"请到网页确认后再重跑 --merge。")
        return False
    if r is None and status == "method_not_allowed":
        # N436：不是内容冲突，别把人推向解冲突 / --force-overwrite。
        print(f"\n❌ PR #{number} 未合并：仓库不允许用 "
              f"「{method}」这种方式合并。")
        print("   这不是内容冲突 —— PR 本身可以合并（GitHub 报告 "
              "mergeable=clean）。")
        print("   改用仓库允许的合并方式重跑，例如：")
        print(f"     python3 push_api.py --merge --method merge")
        print(f"   PR #{number} 与分支 {branch} **已保留**，改动没丢。")
        return False
    if r is None:
        if status == "ci_pending":
            # CI 挡住 ≠ 内容冲突，也 ≠ 分支旧了。
            #
            # 三者的处置完全不同：
            #   conflict   → 拉取主干、手工解决冲突        （本地操作）
            #   need_update→ 网页点 Update branch 或重推   （更新分支）
            #   ci_pending → 只能等 CI 跑完/转绿           （什么都不用做）
            # 把 ci_pending 错当前两类，会把人推向一堆无效操作，
            # 最后怀疑工具坏了。
            print(f"\n⏳ PR #{number} 被 CI 状态挡住（内容无冲突，分支也是最新的）。")
            print("   · 等 CI 跑完 / 转绿后重跑 --merge 即可")
            print("   · 若仓库本就没有必需检查，说明开了分支保护规则，"
                  "需到仓库设置里调整")
            print(f"   · 分支 {branch} 与 PR #{number} 已保留，改动没丢。")
            return False
        if status == "need_update":
            # 不是内容冲突，只是分支旧了 —— 保留分支：
            # 用户可以在 GitHub 网页点「Update branch」就地解决，比本地重推快。
            print(f"\n❌ PR #{number} 暂不能合并：分支 {branch} 不是最新的。")
            print("   仓库开了「合并前分支必须与主干同步」的保护规则，改动本身没冲突。")
            print("\n   二选一：")
            print("     · 网页上点 PR 的「Update branch」后，再跑 --merge")
            print("     · 本地：push_api.py --pull   然后   push_api.py（会用新基点重建分支）")
            print(f"\n   分支 {branch} 与 PR #{number} 已保留，改动也在本地，都没丢。")
            return False
        else:
            print(f"\n❌ PR #{number} 合并冲突：GitHub 无法把 {branch} 自动合进 {BRANCH}。")
            print("   把主干改动合进本地、解决冲突后重新推送：")
            print("     python3 push_api.py --pull        # 三方合并主干改动到本地")
            print("     # 手工解决冲突标记后")
            print("     python3 push_api.py --resolve <文件>")
            print("     （本地已删掉的那些要加 --yes）")
            print("     python3 push_api.py               # 重新推送")
        print("\n   改动没丢 —— 还在你的本地工作区。")
        if keep_on_conflict:
            print(f"\n   分支 {branch} 与 PR #{number} 已保留（--keep-on-conflict）。")
        elif branch == BRANCH or branch in PROTECTED_BRANCHES:
            # N229（第一百轮，中危）：受保护分支在这条路径上原本会走到
            # abandon_task → _refuse_protected_delete → **抛 SystemExit**，
            # 退出码 1。而**推送已经成功了** —— 改动在远端该分支上、
            # PR 也开着，只是合并不进去。
            #
            # 于是同一个"受保护分支不能删"的事实，两条路径给出相反结论：
            #   合并**成功** → N80 那段：打印"保留"，return True，退出码 0
            #   合并**冲突** → 抛异常，退出码 1（= 真出错）
            #
            # 退出码 1 会让 CI/用户以为推送失败而去**重推**，而重推
            # 只会再造一个 PR。文案更错：它说"拒绝删除"，而用户要
            # 的信息是"推送成功了、改怎么合并"。
            #
            # 修法：与 keep_on_conflict 那条**完全同处理** ——
            # 分支保留、改动在本地，退出码一致（不是 1）。
            print(f"\n   {branch} 是受保护分支，合并冲突后**保留**"
                  f"（不删除）。受保护分支：{sorted(PROTECTED_BRANCHES)}")
            print(f"   分支 {branch} 与 PR #{number} 都还在，改动也在本地。")
            print("   把主干改动合进来解决冲突后重跑 --merge 即可。")
        else:
            # 收摊而不是留着：一个合不进去、又没人再推的分支就是僵尸 PR，
            # 会一直占着列表，也会让 --prune 每次都报一遍。
            # yes 必须透传：abandon_task 里「分支无本地记录」那道关靠它
            # 区分「硬拦」与「显式授权」。不传的话 --yes 也会被拦，
            # 于是提示里的「用 --yes 显式删除」成了一句做不到的话 ——
            # 用户照做却仍然失败，比不提示更糟。
            abandon_task(state, branch, number, yes=yes)
            print(f"\n   已关闭 PR #{number} 并删除分支 {branch}（改动在本地，没丢）。")
            print("   想留着分支自己处理：加 --keep-on-conflict")
        return False

    audit({"kind": "merge_ok", "pr": number, "branch": branch,
           "method": method, "status": status})
    if not quiet:
        print(f"✅ PR #{number} 已合并（{status}）")
    # 取出本次 PR 推到分支的内容，让基线能正确跟进（见 _refresh_main_baseline）
    task = (state.get("tasks") or {}).get(branch) or {}
    # 优先用 merge 响应里的 sha：PUT /merge 返回 200 后立即 ref_sha(BRANCH)，
    # 若 GitHub 的 ref 更新有延迟会拿到**旧 head**，后续判定会误以为
    # 「远端又前进了」。
    merged_sha = (r or {}).get("sha") or ref_sha(BRANCH)
    _refreshed = []
    lagging = _refresh_main_baseline(state, merged_sha,
                                     merged_pushed=task.get("pushed"),
                                     refreshed_out=_refreshed)
    # N80：--branch develop 推送并合并成功后，develop 会被**直接删掉**。
    # 可达链路：--branch develop → 只拦 `== BRANCH`（main），
    # develop != main 所以分支名生效 → 建分支 → 推 → 开 PR → 合并成功
    # → 这里 delete_branch(branch)，全程无受保护分支闸门。
    # 而 PROTECTED_BRANCHES 在 prune() 里是排除名单、"别动它"，
    # 在合并成功这条删除路径上却照删不误 —— 同一份常量自相矛盾。
    #
    # 删除长期集成分支不可逆且波及所有人，必须在这里也过同一道闸。
    if branch in PROTECTED_BRANCHES or branch == BRANCH:
        print(f"  · {branch} 是受保护分支，合并后**保留**（不删除）。"
              f"受保护分支：{sorted(PROTECTED_BRANCHES)}")
    else:
        delete_branch(branch)
        if not quiet:
            print(f"  · 已删除分支 {branch}（内容已在 {BRANCH}）")
    state.setdefault("tasks", {}).pop(branch, None)
    if state.get("task_branch") == branch:
        state["task_branch"] = None
        state["pr_number"] = None
    save_state(state)
    # N571（A151）：与 direct 侧那句同源，文案逐字一致 ——
    # 「基线已跟进」这件事两条工作流必须给出同一份说法，否则按工作流
    # 不同会得到不同结论（A151 实测 A 组有、C 组无）。
    # 无条件打印（不套 `if not quiet`）：与下面 lagging 那句对称，
    # 它们不是交互性输出，quiet 静音的是「确认 / 已合并」那一类。
    if _refreshed:
        print(f"\n  · {len(_refreshed)} 个非本次推送的文件远端已更新，"
              f"本地已是同一版本，基线已跟进")
    if lagging:
        print(f"\n  ! {len(lagging)} 个文件本地落后于合并后的 {BRANCH}，改它们前先 --pull")
    return True


def _refuse_protected_delete(branch, action="删除"):
    """删除远端分支前**唯一**的判据。**所有**调用点都必须先过这里。

    N94（第二十八轮）：这里原先写的是「**三处**调用点」，而实际会删除
    远端 ref 的位置有 **5 处**（abandon_task / close_pr /
    delete_branch_cmd / do_merge / _cleanup_failed_branch）。按 3 去核对
    就会漏掉后两处 —— 实测 do_merge 合并成功后能删掉 develop（N80 已补）、
    _cleanup_failed_branch 在 --branch 指定受保护名时同样会删（N94 已补）。
    写死数字的代价就是这个：每新增一个调用点它就过期一次，而过期后
    **看着仍然像在描述全部**。所以改成不写数字。

    为什么要收敛成一个函数：同一个危险动作原本有三份实现
    （abandon_task / close_pr / delete_branch_cmd），各自写了一段
    `if branch == BRANCH`。上一轮只给其中一处补上了 PROTECTED_BRANCHES，
    另外两处照删不误 —— 实测 `--close-pr --yes` 和合并冲突收摊都能删掉
    develop。三份判断必然继续漂移，所以这里收敛成一处。

    判据**不受 --yes 影响**：--yes 是脚本文档自己推荐的用法，而「确认」
    不能授权删除一条长期集成分支 —— 那种删除不可逆，且会波及所有人。
    """
    if branch == BRANCH:
        _blocked(f"拒绝{action}主干分支 {BRANCH}。")
    if branch in PROTECTED_BRANCHES:
        _blocked(
            f"拒绝{action}受保护分支 {branch}。\n"
            f"  受保护分支：{sorted(PROTECTED_BRANCHES)}"
            f"（可用环境变量 PUSH_API_PROTECTED 调整）。\n"
            f"  它们多为长期集成分支，删除不可逆且影响他人；\n"
            f"  确要操作请先从 PUSH_API_PROTECTED 中移除该分支名。")


def abandon_task(state, branch, pr_number=None, yes=False):
    """放弃任务：关 PR + 删分支。

    合并冲突时用。改动本来就在本地工作区，分支只是传输通道，
    删掉它不会丢任何东西；留着反而会变成僵尸 PR 一直占着列表。

    「不会丢任何东西」这个前提**只在单人单分支时成立**：
      · 机器 A 用 --hold 推 v2，机器 B 推 v3
      · A 合并冲突 → 本函数删掉分支 → B 的 v3 永久丢失
    所以删除前要比对分支 head 与 tasks 里记录的内容；对不上就说明
    分支上有别人后来推的改动，删不得。
    """
    # 必须在**任何**远端写操作之前（含 find_pr / PATCH / DELETE）。
    _refuse_protected_delete(branch, "删除")

    # 内容校验：分支上有本地不知道的改动时不删。
    #
    # N226：这里原本是**内联副本**，而 _check_branch_ownership 是抽出来
    # 给三条删除路径共用的同一套判据（N72）。副本没接线 ——
    # 正是 _refuse_protected_delete 的 docstring 批评的形态：
    #   「三份判断必然继续漂移，所以这里收敛成一处」。
    #
    # 漂移**已经发生**：内联版的第二分支不打印「--yes 下直接删除
    # （内容归属未校验）」，而共享版会 —— 同样的操作，走 abandon_task
    # 比走 --close-pr 少一条告知。
    #
    # 改为调用共享版，副本删除。
    _check_branch_ownership(state, branch, "删除", yes)

    pr = pr_number or (find_pr(branch, pr_state="open") or {}).get("number")
    if pr:
        try:
            api("PATCH", f"/pulls/{pr}", {"state": "closed"})
        except SystemExit:
            pass
    delete_branch(branch)
    state.setdefault("tasks", {}).pop(branch, None)
    if state.get("task_branch") == branch:
        state["task_branch"] = None
        state["pr_number"] = None
    save_state(state)


def _check_branch_ownership(state, branch, action, yes,
                            require_record=True):
    """:abandon_task 的内容归属校验，抽出来给三条删除分支的路径共用（N72）。

    两条判据：
      1. tasks[branch].head 与远端 ref 不一致 → 分支上有**别人后来推的**
         改动，删掉就永久丢失 → 硬拒绝。**总是执行**。
      2. tasks 里没有记录 → 无法确认上面有没有别人的改动 → 非 --yes
         时拒绝，--yes 下删除但明确说明「内容归属未校验」。
         仅 `require_record=True` 时执行。

    N301（第八十九轮，中危）：`delete_branch_cmd` 原先只在
    `branch.startswith(TASK_PREFIX)` 时调本函数 —— 于是同一条分支、
    同一份状态、同一个"别人推过"的事实：

        --close-pr --branch pr/backfill --yes  → 硬拒绝
        --delete-branch pr/backfill --yes      → 静默删掉

    差异**仅由分支名前缀**造成。而当初那个条件校验的注释理由
    （"非 task/* 分支本来就没有本地记录，要求必须有记录会把合法清理
    全部挡死"）**只覆盖判据 2**，覆盖不到判据 1。
    注释为判据 2 论证的豁免，被实现成了两条判据一起豁免。

    更糟的是衔接：prune() 早已把前缀白名单改成排除法（本脚本自己承认
    pr/ fix/ feature/ 等前缀的分支确实存在且要纳入体检），
    而 --prune 的输出里每条都给出可直接照抄的命令
    `python3 push_api.py --delete-branch pr/backfill` ——
    体检报出分支 → 用户照抄 → 走的正是没有判据 1 的那条路。

    修法：判据 1 无条件执行（它是**事实问题**，与分支名无关）；
    只有判据 2 受 require_record 控制（它是"要不要有记录"的策略问题）。
    """
    task = (state.get("tasks") or {}).get(branch) or {}
    head = ref_sha(branch)
    if head and task.get("head") and head != task["head"]:
        _blocked(
            f"拒绝{action} {branch}：分支上有未记录的改动。\n"
            f"  本地记录的版本 {task['head']}\n"
            f"  远端实际的版本 {head}\n"
            f"  多半是别人（或另一台机器）在这条分支上推了内容，"
            f"删除会让它永久丢失。\n"
            # N226：这里原先也写「用 --yes 显式操作」，但本分支是
            # **硬拒绝**，--yes 根本绕不过 —— 照着做的人会发现
            # 加了 --yes 照样失败，于是往别处排查（权限？token？），
            # 方向完全跑偏。
            f"  这是硬拒绝：--yes 不能绕过（内容归属问题是事实问题，"
            f"不是确认问题）。\n"
            f"  先 --prune 核对，或手工比对分支内容后再处理。")
    if require_record and not task.get("head"):
        if not yes:
            _blocked(
                f"分支 {branch} 不在本地任务记录里，"
                f"无法确认上面有没有别人的改动。\n"
                f"  确认内容归属后用 --yes 显式操作，或先用 --prune 核对。")
        print(f"  ! {branch} 无本地记录，--yes 下直接{action}"
              f"（内容归属未校验）")


def close_pr(state, branch, yes=False):
    """关闭 PR 并删分支（--hold 的任务确认不做了时用）。"""
    # 本函数末尾会删分支，所以删除侧的两道闸都要过。
    # 原先只有 `branch == BRANCH` 一道，PROTECTED_BRANCHES 没生效 ——
    # 实测 --branch develop --close-pr --yes 直接删掉了 develop。
    _refuse_protected_delete(branch, "关闭/删除")
    # N72：abandon_task 有两道内容归属校验，close_pr / delete_branch_cmd
    # 一道都没有 —— 同一个场景换一条命令就完全没有防护。
    #
    # abandon_task 的 docstring 写明了那两道的必要性：
    # 「不会丢任何东西」只在单人单分支时成立：机器 A 用 --hold 推 v2、
    # 机器 B 推 v3，A 合并冲突删分支 → B 的 v3 永久丢失。
    # 用户改用 --close-pr --yes 时，同样的丢失照样发生，却无任何提示。
    _check_branch_ownership(state, branch, "关闭并删除", yes)
    pr = find_pr(branch, pr_state="open")
    if not pr:
        print(f"{branch} 没有开启中的 PR")
        return
    n = pr["number"]
    if not yes:
        if safe_input(f"关闭 PR #{n}（{branch} → {BRANCH}）并删除分支？(y/N) ").strip().lower() != "y":
            _cancelled()
            return
    api("PATCH", f"/pulls/{n}", {"state": "closed"})
    print(f"✅ 已关闭 PR #{n}")
    delete_branch(branch)
    state.setdefault("tasks", {}).pop(branch, None)
    if state.get("task_branch") == branch:
        state["task_branch"] = None
        state["pr_number"] = None
    save_state(state)
    print(f"  · 已删除分支 {branch}")


def delete_branch_cmd(state, branch, yes=False):
    """显式删除分支。

    --prune 只报告不删，真正要删走这里，且必须逐个确认 ——
    分支是唯一可能存着「没合进主干的改动」的地方，删错不可恢复。
    """
    # 主干 + 受保护分支：两道闸都在 _refuse_protected_delete 里，
    # 与 abandon_task / close_pr 共用同一份判据（见那里的说明）。
    #
    # PROTECTED_BRANCHES 在 prune() 里是**排除名单**（体检时不纳入），
    # 早先却从没在删除侧生效 —— 同一份常量在报告侧说「别动它」、
    # 在删除侧照删不误（实测：--delete-branch develop --yes 直接删掉）。
    #
    # 必须**不受 yes 影响**：--yes 是脚本文档自己推荐用法，而「确认」
    # 不能授权删除一条长期集成分支 —— 那种删除不可逆，且会波及所有人。
    _refuse_protected_delete(branch, "删除")
    # N72：与 close_pr / abandon_task 同一道内容归属校验。
    #
    # N301：**无条件调用**，只用 require_record 区分前缀差异。
    # 原先写成 `if branch.startswith(TASK_PREFIX):` 整段跳过 ——
    # 于是判据 1（"分支上有别人后来推的改动" → 硬拒绝）也被一起豁免，
    # 而那条是**事实问题**，与分支叫什么名字毫无关系。
    #
    # 保留的豁免只有判据 2：非 task/* 分支（别人的、手工建的）本来
    # 就没有本地记录，对它们要求「必须有记录」会把合法清理全部挡死。
    _check_branch_ownership(state, branch, "删除", yes,
                            require_record=branch.startswith(TASK_PREFIX))
    if not branch.startswith(TASK_PREFIX):
        print(f"  ⚠️  {branch} 不是 {TASK_PREFIX}* 任务分支"
              f"（可能是受保护分支或别人在用的分支）")
        if not yes and safe_input("  确认仍要删除？(y/N) ").strip().lower() != "y":
            _cancelled()
            return
    pr = find_pr(branch)
    if not pr:
        note = "没有任何 PR（孤儿分支，删除会丢改动！）"
    elif pr.get("merged_at") or pr.get("merged"):
        note = f"PR #{pr['number']} 已合并，内容已在 {BRANCH}（删掉无损失）"
    else:
        note = f"PR #{pr['number']} 状态 {pr.get('state')}（未合并，删除会丢改动！）"
    print(f"\n删除分支 {branch}")
    print(f"  {note}")
    if not yes:
        if safe_input("确认删除？(y/N) ").strip().lower() != "y":
            _cancelled()
            return
    delete_branch(branch)
    state.setdefault("tasks", {}).pop(branch, None)
    if state.get("task_branch") == branch:
        state["task_branch"] = None
        state["pr_number"] = None
    save_state(state)
    print(f"✅ 已删除 {branch}")


def _list_conflict_artifacts():
    """列出 .push-conflicts 里的残留副产物。

    这个目录**只增不减**：删除只在 --resolve 和下一轮冲突写入前的清理里。
    用户放弃解决冲突，它就永久留下 —— 在仓库外、--prune 不管、也没有
    任何命令能列出来。而里面是**完整的远端明文内容**，属于被遗忘的
    敏感数据副本。至少要让 --status / --prune 能看见它。
    """
    d = CONFLICT_DIR
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                out.append((name, os.path.getsize(p)))
            except OSError:
                out.append((name, -1))
    return out


def _list_legacy_conflict_artifacts():
    """N535（第一百七十五轮，低危）：旧位置（ROOT 的父目录）里的副产物。

    `_list_conflict_artifacts()` 只读 CONFLICT_DIR（**新**位置），于是
    M79 换目录之后，旧位置里装的远端明文在 `--status` / `--prune` /
    `--audit` 里**全部不可见** —— 而 `_list_conflict_artifacts` 自己的
    docstring 立过要求：「里面是完整的远端明文内容……至少要让
    --status / --prune 能看见它。」换目录时这条要求没跟着搬。

    于是整整一类远端明文副本**目前且仅在** `--clean-conflicts` 里、
    且仅靠那条一次性迁移提示被发现。

    只读、不搬、不删：那个目录可能是同父目录下**另一个仓库**的
    （M79 不自动搬的实证理由），所以这里只报告，并注明需确认归属。
    """
    _old = _legacy_conflict_dir()
    if not _old or not os.path.isdir(_old):
        return []
    try:
        if os.path.abspath(_old) == os.path.abspath(CONFLICT_DIR):
            return []
        names = sorted(os.listdir(_old))
    except OSError:
        return []
    out = []
    for name in names:
        p = os.path.join(_old, name)
        if os.path.isfile(p):
            try:
                out.append((name, os.path.getsize(p)))
            except OSError:
                out.append((name, -1))
    return out


def _report_conflict_artifacts():
    items = _list_conflict_artifacts()
    # N535：旧位置也要报。原先 `if not items: return` 在**新位置为空**
    # 时直接返回 —— 而旧位置有货恰恰是新位置为空的典型场景（升级后
    # 还没在新位置发生过冲突），于是它永远报不出来。
    legacy = _list_legacy_conflict_artifacts()
    if not items and not legacy:
        return
    if items:
        print(f"\n  ! 冲突副产物目录还有 {len(items)} 个文件未清理：{CONFLICT_DIR}")
    for name, size in items[:10]:
        print(f"     · {name}" + (f"  ({size} B)" if size >= 0 else ""))
    if len(items) > 10:
        print(f"     … 还有 {len(items) - 10} 个")
    print("     里面是完整的远端明文内容。冲突解决完请 --resolve，"
          "或确认无用后手工删除该目录。")

    # 给出清理入口：原先只能看、不能清，于是「被遗忘的远端明文副本」
    # 一旦产生就永久留下。既然报了，就该同时说怎么清。
    if items:
        print("     清理已解决冲突的残留副本："
              "python3 push_api.py --clean-conflicts")

    # N535：旧位置同上，只是**不自动搬、不自动清** —— 它可能是同父
    # 目录下另一个仓库的素材（M79 实证场景）。原先它在 --status /
    # --prune 里完全不可见，而里面同样是完整的远端明文。
    if legacy:
        _old = _legacy_conflict_dir()
        print(f"\n  ! 旧位置（本版本之前的目录）还有 {len(legacy)} 个文件：{_old}")
        for name, size in legacy[:10]:
            print(f"     · {name}" + (f"  ({size} B)" if size >= 0 else ""))
        if len(legacy) > 10:
            print(f"     … 还有 {len(legacy) - 10} 个")
        print("     里面同样是完整的远端明文内容。"
              "确认属于本仓库后再手工处理（它可能是同父目录下另一个仓库的）。")

def clean_conflict_artifacts(state=None, yes=False):
    """清理 .push-conflicts 里的残留副产物（含远端明文）。

    为什么需要显式入口：这个目录只增不减，删除只发生在 --resolve。
    但用户有两条完全正当的路径不会走 --resolve：
      · 手工改好文件后直接推（工作区已解决，只是没声明）
      · 放弃那次改动，重跑 --pull
    这两条路都会让副产物永久留下 —— 在仓库外、--prune 不管，
    原先也没有任何命令能清。而里面是**完整的远端明文**。

    安全边界：**只清「已解决」的**。
    仍在 state["conflicts"] 里的文件，其 .base / .remote 是用户正在
    对照的素材，删掉等于毁掉解决冲突的依据。所以默认跳过它们，
    除非用户显式 --yes 且该文件工作区已无冲突标记。

    返回 (已删, 跳过)。
    """
    d = CONFLICT_DIR
    # M79：目录位置从「ROOT 的父目录」改到了 <ROOT>/.git/。升级后新
    # 位置为空是**正常的**，而旧位置可能还装着上一版留下的副产物
    # （里面是远端明文）。提示迁移，**不自动搬** —— 那个目录可能是
    # 同父目录下**别的仓库**的（M79 实证场景），搬进来等于把别仓库
    # 的素材搬进本仓库。
    #
    # 放在 isdir 判断**之前**、无条件执行：原先挂在「新位置不存在」
    # 那一支里，于是只要本仓库此后**发生过一次冲突**（_conflict_path
    # 会顺手建出新目录）就再也不提示了 —— 而恰恰是那些仓库才有旧
    # 副产物。最该被告知的人被排除在外，是判据选错对象的又一个例子。
    _warn_legacy_conflict_dir(d)
    if not os.path.isdir(d):
        print("  · 没有冲突副产物目录，无需清理。")
        return 0, 0
    # _list_conflict_artifacts 返回 [(文件名, 大小)]，文件名形如
    # "a.txt.base" —— rel 要去掉后缀反推，它本身不返回 rel 字段。
    try:
        items = _list_conflict_artifacts()
    except OSError as e:
        print(f"  ! 读取冲突副产物失败：{e}")
        return 0, 0
    if not items:
        print("  · 冲突副产物目录为空，无需清理。")
        return 0, 0

    pending = set((state or {}).get("conflicts") or [])
    # N106：`yes` 是**死参数** —— docstring 承诺"除非用户显式 --yes
    # 且该文件工作区已无冲突标记"，但函数体从未用过它，调用点也不传。
    # 于是 --clean-conflicts --yes 里的 --yes 被静默忽略，
    # 未解决文件（但用户其实已手工改好、只是没 --resolve）的副本
    # 永远清不掉，而里面是远端明文。
    #
    # 补上这条路：--yes 时，若该文件的**工作区内容已无冲突标记**，
    # 视作已解决可清理（这正是 docstring 承诺的语义）。
    if yes and pending:
        _still = []
        for _rel in list(pending):
            try:
                # N339（第一百〇六轮，中危）：原来是
                #     _data = _fh.read(65536)
                #     ..._has_conflict_markers(_data[:65536], strict=True)
                #
                # **截断两次**（read 已截过、[:65536] 再截一次 ——
                # 冗余本身就说明没意识到前者截过）。
                #
                # 实证：70051 字节的文件、冲突块在末尾（70000 之后）
                #     resolve() 用全量        → True（有冲突）
                #     clean 用前 64KB         → False（判已解决）
                #     ★ 同一份文件两处结论相反
                #
                # 后果：`--clean-conflicts --yes` 对这类文件判**已解决**
                # → 删掉它的 .base / .remote。而 clean_conflict_artifacts
                # 自己的 docstring 写着"仍在 state['conflicts'] 里的文件，
                # 其 .base/.remote 是用户正在对照的素材，删掉等于毁掉
                # 解决冲突的依据"。
                #
                # 而此时工作区里的冲突标记**还在**（resolve() 会拒绝它）
                # —— 副本没了、残缺内容还在，等着被推上去。
                #
                # 与 N254 / N288 的关系要说清：那两次都只统一了
                # **函数**，没统一**喂进去的字节**。于是
                #   N254 前：判据不同 → 结论相反
                #   N288 后：判据相同、输入不同 → 结论照样相反
                #
                # 修法：改用 _local_bytes（与 resolve() 同源、全量）。
                # 大文件多读一点字节的代价，远小于误删对照素材。
                _data = _local_bytes(_rel)
                # N254：这里与 resolve() 的判据不同步 ——
                # resolve 已按行判且认 =======，这里只查 <<<<<<</>>>>>>>。
                # 于是"只删了头尾标记、中间 ======= 还在"的残缺文件
                # 会被判成**已解决** → 副产物被清理 →
                # 唯一的对照依据没了，而残缺内容还在工作区里等着被推上去。
                #
                # 统一走 _has_conflict_markers（同一份判据）。
                # N288：pending 来自 state["conflicts"]，
                # 即已知有冲突史 → strict=True，行首 ======= 也认。
                if _has_conflict_markers(_data, strict=True):
                    _still.append(_rel)
            except OSError:
                _still.append(_rel)      # 读不了 → 当未解决，保守
        if _still:
            print(f"  · {len(_still)} 个文件工作区仍有冲突标记，"
                  f"保留其副产物（先解决再清理）：{_still[:3]}")
        pending = set(_still)
    removed, skipped = [], []
    seen = set()
    for name, _size in items:
        # M68（A75）：原先这里是 `rel = name[: -(len(kind) + 1)]`
        # —— 只去掉后缀，**没有还原编码**。而写入侧（`_conflict_path`）
        # 是把 `/` 压平成 `__` 之后再落盘的，于是
        #     sub/a.txt  → 落盘 "sub__a.txt.remote"
        #     ↑ 反推     → 得 "sub__a.txt"（真 rel 是 "sub/a.txt"）
        #     ↑ 再编码   → "sub___a.txt.remote"（目录里根本没有这个名字）
        #
        # 两个 bug 互相抵消，净效果是**清不掉**而不是删错（方向保守），
        # 但后果一样：子目录下的副产物永远留在磁盘上，而里面是完整的
        # 远端明文。用户看到「无需清理」，以为清过了。
        #
        # 改用 `_conflict_rel_from_name()`（编码的真正逆运算，带往返校验）。
        rel = _conflict_rel_from_name(name)
        if rel is None:
            # 认不出（后缀不对、或反推校验不过）的文件不动：
            # 可能是用户自己放的，脚本不该猜着删
            # （与「删除不可逆」的一贯取向一致）
            skipped.append((name, "无法识别或无法反推原路径，未处理"))
            continue
        # N402（第一百二十九轮，中危）：`seen` 的键原来是 rel，而删除
        # 是按**磁盘上真实的文件名**（下面的 `_stem`）做的。升级前后
        # 两种编码（旧 `__` 压平 / 新百分号转义）会反推出**同一个** rel，
        # 于是第二条被这里吃掉 —— 实测同一个 rel 的四份副本
        #（sub%2Fa.txt.{remote,base} + sub__a.txt.{remote,base}）一次只清掉
        # 2 个，而屏幕说「✓ 已清理 2 个」，里面剩的是完整的远端明文。
        #
        # 更麻烦的是这个共存**是代码自己造出来的**：写新副本前的
        # 「清掉上一轮残留」只认新格式，旧副本永远不会被预清理，
        # 于是每次冲突复发都重新凑出这一对。
        #
        # 去重键改用 stem（磁盘文件名去后缀）——它才是删除动作的粒度。
        _stem_seen = name.rsplit(".", 1)[0]
        if _stem_seen in seen:
            continue
        seen.add(_stem_seen)
        # 该文件的冲突还没解决 → 保守跳过
        if rel in pending:
            skipped.append((rel, "冲突未解决，.base/.remote 仍在用于对照"))
            continue
        # M67（A75）：删**磁盘上真实存在的那个文件名**，而不是拿 rel
        # 重新编码出的路径 —— 两者在升级前后**不是同一个名字**
        #（旧 `__` 压平 vs 新百分号转义）。
        #
        # 重新编码去找，会把升级前落盘的旧副本整个漏掉：
        #     rel="sub/a.txt" → 新编码 sub%2Fa.txt.remote
        #     磁盘上却是       sub__a.txt.remote      ★ 永远删不到
        # 而里面是远端明文，漏掉的代价正是 M68 要防的那件事。
        #
        # 配对（.remote / .base）按同一个 stem 现拼，两种编码格式都适用。
        _stem = _stem_seen
        for kind in ("remote", "base"):
            pth = os.path.join(d, _stem + "." + kind)
            if os.path.isfile(pth):
                try:
                    os.unlink(pth)
                    # N403：写**磁盘上真实删掉的那个名字**，不是反推出的
                    # rel —— 新格式下磁盘是 `sub%2Fa.txt.remote`，而 rel
                    # 是 `sub/a.txt`，屏幕写后者用户核对时目录里找不到。
                    removed.append(os.path.basename(pth))
                except OSError as e:
                    skipped.append((rel, f"{kind} 删除失败：{e}"))
    # 目录空了就顺手删掉
    try:
        if not os.listdir(d):
            os.rmdir(d)
            removed.append(f"（目录 {d} 已空，已删除）")
    except OSError:
        pass

    if removed:
        print(f"\n  ✓ 已清理 {len(removed)} 个冲突副产物：")
        for r in removed[:20]:
            print(f"     - {r}")
        if len(removed) > 20:
            print(f"     … 另有 {len(removed) - 20} 个")
    if skipped:
        # M103（A103，dogfood 实测）：标题原先写 `（{skipped[0][1]}）`，
        # 即拿**第一个**条目的原因当总原因，而每个条目后面又各带一次
        # 原因 —— 于是只有 1 个条目时，同一句原因出现两遍，
        # 再算上上面「保留其副产物」那句，同一件事说了三遍。
        #
        # 更实质的问题：条目原因**可能不同**（"无法识别" / "冲突未解决"
        # / "删除失败"），标题写第一个等于暗示其余也是这个原因。
        # 原因本就逐条写着，标题不该替它们概括。
        print(f"\n  · 跳过 {len(skipped)} 个：")
        for rel, why in skipped[:10]:
            print(f"     - {rel}（{why}）")
        print("     解决这些冲突用：python3 push_api.py --resolve <文件>")
        print("     本地已删掉的那些（放弃改动、保留远端那份）要加 --yes："
              "python3 push_api.py --resolve <文件> --yes")
    if not removed and not skipped:
        print("  · 无需清理。")
    return len(removed), len(skipped)


def show_status(state, head_sha, as_json=False):
    """--status。as_json=True 时输出结构化结果而不是人读文本。

    这是 Ui 层分离的**探路**：只在只读子命令上做。

    为什么不一口气全做：推送路径有大量人工确认与交互文案，半结构化的
    输出比没有更危险 —— 人会拿它当可靠依据，而它其实只覆盖了一半。
    先把只读这条走通（数据从哪来、怎么渲染、测试怎么断言），
    再往写路径推广才有参照。
    """
    # _report_conflict_artifacts() 自己直接 print —— 在 JSON 模式下它会把
    # 提示插到 JSON **前面**，导致输出不可解析（实测：输出以 "\n  ! 冲突副
    # 产物目录..." 开头，json.loads 直接失败）。
    #
    # 这正是「决策 / 输出混写」的代价：纯数据消费者被迫去剥人类文案。
    # 彻底修法是把 _report_conflict_artifacts 改成返回待报告项、由 Ui 决定
    # 怎么渲染 —— 但那要动它的全部调用点，属于阶段 2 的事。
    # 阶段 0 先捕获其输出并作为 warnings 字段，既让 JSON 可解析，
    # 也不丢信息。
    # 插件状态要说出来：删除功能「没生效」和「没装」对用户的意义完全不同，
    # 而两者在界面上都表现为「什么都没发生」。不吭声等于让人无从下手。
    _note = _plugin_note()
    if _note and not as_json:
        print(f"  ! {_note}")

    # N173：文本与 JSON **共用一份**清单，只算一次。
    #
    # N181：#40 为了消除"算两遍"把计算只留在 `if not as_json:` 块里，
    # 于是 **JSON 模式永远不计算**，_pd_json 保持 None → 最终置 []，
    # pending_deletes.count 恒为 0。
    # 而 JSON 恰恰是 AI / CI 读取待删除清单的**唯一可靠途径**
    #（文本块会被忽略）—— 这条回归让那个字段彻底失效。
    #
    # 修法：计算无条件执行；只有"打印成文本块"这一步按 as_json 区分。
    _pd_json = None
    # N636（接上）：远端树是否没取到。**必须在本块之外先落一个默认值** ——
    # 它在下面的 try 里赋值，而 `local_state_map()` 若先抛异常，控制流会
    # 直接跳到 except，`_rs_fail` 就从未被绑定；那时下面读它会 NameError，
    # 且大概率被外层宽 except 吞掉（与 N112/N114/N305/N326 同族，本批
    # 已踩过多次："引用了可能未绑定的名字 + 被宽 except 吞掉"）。
    _rs_fail = False
    # N349：待删除检测的**失败原因**暂存处。
    # 必须先于计算定义：计算点在 `warnings` 建好之前，直接 append 会
    # NameError 并被宽 except 吞掉（那样比不报错更糟：从"多报"变"漏报"）。
    _pd_fail = []
    # 待删除清单：--status 是使用者查待办的主入口，必须能看到。
    # 混在推送输出里会被漏看，落到只读命令里才是可靠出口。
    if True:
        # **实时计算**而不是只读落盘值：无待推送改动那次运行会提前返回，
        # 根本走不到落盘 —— 只读落盘值会让 --status 永远看不到清单，
        # 而它本该是查待办的主入口。
        try:
            _lmap = local_state_map()
            # 直接自取，不引用形参：show_status 有多个调用点，
            # 逐个改签名传 rstate 牵动面太大，而 --status 是只读命令，
            # 多一次 tree 请求可以接受（且走进程内缓存）。
            # N636（A133，中危）：`_rs` 与整块计算写在**同一个 try** 里，
            # 于是远端树一取不到，B 源也跟着被带掉 —— 而 B 源
            # （`_detect_deletions`，只读 `git status`）**零网络请求**，
            # 根本不需要远端。
            #
            # 实测（本轮复现，真实 git 仓库 + 只替换 remote_state_map）：
            #   场景 E  远端取不到 + 本地 `rm b.txt`（未 commit，只有 B 源看得见）
            #     → degraded=True, **count=0** ★
            #     而同一时刻 `pending_deletes_union(state, lmap, None)` = ['b.txt']
            #     —— 数据就在本地，问一声就有，却因为远端那次调用先失败
            #        而整块跳过。
            #   场景 H  远端取不到 + 本地无删除 → degraded=True, count=0 ✓
            #
            # 所以把远端单独取：取不到只置 `_rs_fail`，B 源照算。
            #
            # ⚠ 最直觉的修法（只把 `remote_state_map` 包一层 try）会让
            # M51 **倒退**：包了 try 之后 `_pd_json` 不再是 None，
            # 而 `degraded` 的判据是 `_pd_json is None` → 恒 False。
            # 后果在场景 H 上看得最清楚：远端取不到、本地无删除 →
            # `degraded=False, count=0`，消费者读到「确实没有待删」，
            # 实际是「不知道」—— 正是 M51 的注释逐字写着要消灭的形态。
            # 所以 `degraded` 必须改成 `_rs_fail or (_pd_json is None)`。
            _rs = None
            _rs_fail = False
            try:
                _rs = remote_state_map(head_sha)
                if _rs is None:
                    _rs_fail = True
            except SystemExit as e:
                _rs_fail = True
                if not as_json:
                    print(f"  ! 待删除检测中止：{e}")
                else:
                    _pd_fail.append(f"待删除检测中止：{e}")
            except Exception as e:
                _rs_fail = True
                if not as_json:
                    print(f"  ! 待删除检测失败：{type(e).__name__}: {e}")
                else:
                    _pd_fail.append(f"待删除检测失败：{type(e).__name__}: {e}")
            # N334（第一百〇三轮，中危）：这里原本只算 A 源，
            # B 源（_detect_deletions）在下面 6588 / 6704 两处各算一遍
            # —— 文本一处、JSON 一处，**两次 git 子进程、两份判据**。
            # 而 6675 行 N173 的注释写着"现在只算一次，文本与 JSON 共用"，
            # **并没有**。
            #
            # 两条支路的 rstate 判据写法还不同：
            #   文本侧  `if _rstate_now:` 才过滤（拿不到远端 → 不过滤）
            #   JSON 侧 无条件过滤（拿不到远端 → 过滤成空）
            # 于是远端树取不到时（大仓库截断，正是 N68 惰性化的场景）：
            #   文本报 1 个 / JSON 报 0 个 → **结论相反**。
            # 而 `pending_deletes_union()` 在同场景返回与文本侧一致。
            #
            # 这个字段花 N284、N305 两轮才让两侧一致，
            # 现在换一个触发条件（远端树取不到）原样复活 ——
            # 而远端树取不到恰恰是 `--status` 最需要工作的场景。
            #
            # 改用共享函数**一次算完**，文本与 JSON 各取同一份结果。
            _pd = pending_deletes_union(state or {}, _lmap, _rs)
            _pd_json = list(_pd or [])
            # 只有文本模式在这里打印；JSON 模式由 payload 输出。
            if _pd and not as_json:
                report_pending_deletes(_pd, "--status")
                # N145：这行是**只存在于内存**的写入 —— show_status
                # 全程无 save_state，调用后直接 return。
                # N110 刚把文案从"已记入基线"改成"每次实时计算"，
                # 这里却仍在"记"，方向与文案相反：
                # 下一位维护者看到会以为有持久化。删掉。

        # N121：SystemExit **不是** Exception 的子类，只写 except Exception
        # 接不住它。而 remote_state_map 在「远端 tree 被截断」时正是抛
        # SystemExit —— 于是 --status 这个只读命令整个崩掉
        #（实测：抛 "远端 tree 被截断…已中止"）。
        # 同一函数 5417 行就正确地接了 SystemExit，这里却漏了。
        # N181 附带：计算现在无条件执行，而失败时的 print 会把文本
        # 插进 JSON **前面**，导致输出不可解析（实测 test_config 的
        # status_json_is_parseable 直接红）。
        # JSON 模式下失败只能静默 —— 详情已不可得，但绝不能污染输出。
        # 这与 _report_conflict_artifacts 的处理同一道理：
        # 纯数据消费者不该被迫去剥人类文案。
        except SystemExit as e:
            if not as_json:
                print(f"  ! 待删除检测中止：{e}")
            # N349（第一百〇九轮，中危）：JSON 侧**没有失败通道**。
            #
            # 上面那段注释写着"warnings 是现成的载体，填上"，
            # 而 `if _pd_json is None: _pd_json = []` 下面是空的 ——
            # 于是「检测失败」与「确实没有待删」在 JSON 里不可区分
            # （实测两者都是 count=0 + warnings=[]）。
            #
            # 而 pending_deletes 字段的注释原文写着"文本块会被忽略，
            # 机器可读字段不会 —— AI/CI 靠这个**可靠拿到**要删哪些"。
            # 唯一被承诺可靠的那一侧，在它自己宣称已修的失败场景下
            # 返回"没有"，且不带任何"我没算出来"的信号。
            #
            # 失败原因（远端树取不到）恰恰是 N68 惰性化专门要照顾的
            # 大仓库场景 —— 越是需要 --status 到场的时候它越安静。
            # 方向是 fail-open（漏删不是误删），所以只报错不阻断。
            #
            # ⚠ 不能在这里直接 `warnings.append(...)`：warnings 在下面
            # 才定义（L6946/6949），此处引用会 NameError，被外层宽
            # except 吞掉后从"多报"变成"漏报"（更糟）。
            # 与 N112/N114/N305/N326 同族 —— 我已经第四次踩这个坑。
            # 所以先收进 _pd_fail，等 warnings 建好再合并。
            else:
                _pd_fail.append(f"待删除检测中止：{e}")
        except Exception as e:
            if not as_json:
                print(f"  ! 待删除检测失败：{type(e).__name__}: {e}")
            else:
                _pd_fail.append(f"待删除检测失败：{type(e).__name__}: {e}")

    # N33：--status 此前完全不展示待删除项。
    # 于是开启删除的用户，在按下 y 之前**没有任何途径**知道本次会删什么：
    #   --status   不显示（本处）
    #   --dry-run  早退早于删除计算（N13 已修）
    #   推送过程   能显示，但在按 y 授权之后（N9 已修）
    # 讽刺的是删除功能**关闭**时反而能看到列表（"仍留在远端"那一段）。
    # N284：这段被包在 `if not as_json:` 里，于是 JSON **只拿到 A**
    # （pending_deletions），而 A 对「未 commit 的删除」恒盲 ——
    # lmap 以 git 索引为唯一权威，rm 掉但没 git rm 的文件索引里仍在。
    #
    # 两个来源恰好互补、各自看不见另一半：
    #   A pending_deletions  看不见未 commit 的删除
    #   B _detect_deletions  看不见已 commit 的删除（索引已更新，
    #                        但它读的是 git status，commit 后为空）
    #
    # 实测：rm a.txt（未 commit）→ JSON count=0，而文本报 1 个。
    # 读文本的人与读 JSON 的机器得到**相反结论**。
    # 而该字段的注释承诺"自动化场景（AI/CI）靠这个可靠拿到"。
    #
    # 修法：两段都算，JSON 取并集。B 侧补 `p in rstate`（N285）。
    #
    # N306 / N334：并集逻辑抽成了 `pending_deletes_union()`，
    # 出口登记 / 出口重算 / 本处**三处共用同一份** ——
    # 原先三处各写各的，正是 N284 只修一处、另两处照旧的成因。
    #
    # N334：本处此前是**内联副本**（注释说三处共用，实际只有两处调了），
    # 且文本与 JSON 各调一次 `_detect_deletions()`（两次 git 子进程）。
    # 现在 `_pd` 已经是 A∪B，文本与 JSON 都从它派生，
    # B 源**只算一次** —— 与 N173 那条注释终于一致。
    #
    # 文本侧要单独显示"B 源贡献了哪些"（未 commit 的删除），
    # 所以从 _pd 里反推：A 源看不到的那部分就是 B 的贡献。
    # N334 连带：这段必须**无条件**计算，且要在 _dd_all 之前 ——
    # _dd_all 现在从 _pd 反推 B 的贡献（要减掉 A 源），
    # 而 A 源需要 _rstate_now。
    #
    # 放在它后面的话 NameError 被 `except BaseException` 吞掉，
    # 症状是"文本侧永远不显示 B 源"，排查方向完全错。
    # 这类"引用了尚未定义的名字 + 被宽 except 吞掉"我自己踩过
    # 三次（N112 / N114 / N305），此处显式提前。
    # N508（第一百六十六轮，低危）：「取不到」与「取到了、是空的」在这里
    # 被合并成同一个 `{}`，下游 `if _rstate_now:` 于是把**空仓库**报成
    # 「远端树取不到」。
    # 这是下面那段注释点名要防的同一类错误的**镜像**：那边是拿
    # 「没有」冒充「没算出来」，这边是拿「没算出来」冒充「算出来了、是空的」。
    # 两条都是「声明与实际不符」，只是方向相反。
    # 用独立的 `_rs_ok` 分开；`_rstate_now` 本身仍一律是 dict，下游不变。
    _rstate_now = _rs if "_rs" in dir() else None
    _rs_ok = True
    if not _rstate_now:
        try:
            _rstate_now = remote_state_map(head_sha)
            _rs_ok = _rstate_now is not None
        except BaseException:
            _rs_ok = False
        _rstate_now = _rstate_now or {}
    _dd_all = []
    try:
        _a_only = set(pending_deletions(state or {}, _lmap or {},
                                        _rstate_now) or [])
        _dd_all = [p for p in (_pd or []) if p not in _a_only]
    except BaseException:
        _dd_all = []
    # N285：文本那一路原本报「远端仍在」，却**从不查远端** ——
    # 只过滤了「基线里有」。远端已经删掉的文件会被算成待删除，
    # 用户照着去删，发现根本没有；幽灵条目还会挤爆熔断配额。
    #
    # N305：这段必须**无条件**计算，不能留在 `if not as_json:` 里 ——
    # JSON 侧也要用它。留在里面的话 as_json=True 时 _rstate_now 未定义，
    # NameError 被下面那层 `except BaseException: pass` 静默吞掉，
    # 于是 B 源整个丢失 → JSON 从"多报"变成"漏报"（更糟：该删的不报）。
    # 这类"引用了尚未定义的名字 + 被宽 except 吞掉"最难发现，
    # 我自己上一轮就踩过（N112 / N114 同族）。
    if not as_json:
        try:
            _dd = _dd_all
            if _rstate_now:
                _dd = [p for p in _dd if p in _rstate_now]
            if _dd:
                print(f"  · 待落实删除 {len(_dd)} 个（本地已删、远端仍在）：")
                for _p in _dd[:10]:
                    print(f"      - {_p}")
                if len(_dd) > 10:
                    print(f"      … 其余 {len(_dd) - 10} 个")
                if _deletes_disabled():
                    print("     删除功能当前关闭，本次推送不会删除它们。"
                          "开启方式：" + _delete_how_to_enable().split("\n")[0])
                    _print_how_to_enable(6)
        except SystemExit as _e:
            print(f"  ! 删除预览中止：{_e}")
        except Exception as _e:
            print(f"  ! 删除预览失败：{type(_e).__name__}: {_e}")

    if as_json:
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _report_conflict_artifacts()
        warnings = [l.strip() for l in _buf.getvalue().splitlines() if l.strip()]
        # N349：把待删除检测的失败原因并进来（它算在本函数更前面，
        # 那时 warnings 还不存在，只能先存 _pd_fail）。
        warnings.extend(_pd_fail)
        # M81-⑤：权限警告在 json 模式下被 `_warn_mode` 收起来了
        # （它打在分派前，print 出来会让输出不可解析）。这里并入，
        # 机器照样拿得到，且不再打断解析。
        warnings.extend(_drain_json_warnings())
    else:
        _report_conflict_artifacts()
        warnings = []
    # N54：基线健康度。
    # 「61 条空记录」那类问题之所以长期没人发现，是因为**没有任何命令
    # 能看到它** —— 它们既不报错也不显示，只在让判据失效时才显形，
    # 而那时症状已经是「推送被莫名拦下」，排查方向完全跑偏。
    # --status 是查待办的主入口，健康度放这里才有人看。
    _health = {}
    try:
        _files = (state or {}).get("files") or {}
        _rs2 = remote_state_map(head_sha)
        _ghost = sorted(p for p in _files if p not in _rs2)
        # 空记录：dict 但没有 sha。它通过 isinstance 守卫永久存活
        # （空 dict 也是 dict），是「假拦截」的主要来源。
        _empty = sorted(p for p, v in _files.items()
                        if isinstance(v, dict) and not v.get("sha"))
        _health = {"baseline_files": len(_files),
                   "ghost_paths": len(_ghost),
                   "ghost_sample": _ghost[:5],
                   "empty_records": len(_empty)}
        if not as_json and (_ghost or _empty):
            print(f"  ! 基线健康度：{len(_files)} 条记录中，"
                  f"{len(_ghost)} 条远端已不存在（幽灵），"
                  f"{len(_empty)} 条无 sha（空记录）")
            if _empty:
                print("     空记录会让文件级校验永久失效且不报错，"
                      "重建基线可清除：--init-baseline")
            if _ghost:
                print(f"     幽灵路径示例：{_ghost[:3]}")
    except SystemExit:
        # remote_state_map 拿不到远端树会抛 SystemExit，而它**不是**
        # Exception 的子类 —— 只写 except Exception 会让它穿透出去，
        # 把 --status 这个只读命令整个搞挂（实测：test_config 的
        # status_json_is_parseable 用假 sha 调用，直接 404 退出）。
        # 健康度是告知性信息，拿不到就不展示，绝不能阻断只读命令。
        _health = {}
    except Exception:
        _health = {}

    # N173：待删除清单此前**算了两遍** ——
    # 文本路径一处（if not as_json 块内）、JSON 路径一处（这里），
    # 且失败降级口径不同（前者 except SystemExit 会打印"中止"，
    # 后者 except BaseException 静默置空）。
    # 同一份信息两种失败表现，改其中一处必漏另一处。
    #
    # 现在只算一次，文本与 JSON 共用：文本块直接打印，
    # JSON 把它收进 payload。失败表现也只有一种。
    # M51（A55，P3）：先把「没算出来」这个事实记下来**再**兜底 ——
    # 兜底成 [] 之后就再也分不清「确实没有」与「取不到」了。
    #
    # 实测（同一份 state，唯一变量是 remote_state_map 是否抛 SystemExit）：
    #   场景A 远端树取不到 → pending_deletes = {"count": 0, "paths": []}
    #   场景B 确实没有待删 → pending_deletes = {"count": 0, "paths": []}
    #   → **逐字相同**
    #
    # 而本字段的注释原文写着「自动化场景（AI / CI）靠这个**可靠**拿到
    # 「哪些要删但没删」」。AI/CI 读到 count=0 会认为"没有要删的"，
    # 实际是"不知道"。
    #
    # 有人会问：失败原因不是已经进 warnings 了吗（N349）？
    # 是，但那要求消费者去 warnings 里做**文案匹配**才能还原这个事实 ——
    # 而本字段的存在理由恰恰是"机器可读，不用剥人类文案"。
    # 让被承诺可靠的那一侧自己带信号，比让消费者跨字段拼事实可靠。
    # N636（接上）：`degraded` 的判据不能只看 `_pd_json is None` ——
    # 远端单独取之后，即使远端挂了 `_pd_json` 也是个 list（B 源照算了），
    # 只看 None 会让 `degraded` 恒 False，M51 要消灭的形态原地复活
    # （场景 H：远端挂 + 无删 → 报「确实没有」，实际是「不知道」）。
    #
    # 语义（必须在注释里写清，见 A133 §3.4）：
    #   degraded=True → **A 源未知**（已 commit 的删除那一路没算），
    #                   count / paths 是**下限**不是全集；
    #   degraded=False → 两源都算过，count 是全集。
    _pd_degraded = _rs_fail or (_pd_json is None)
    if _pd_json is None:
        # N284：JSON 没有失败通道。这里静默置空会让"检测失败"与
        # "确实没有待删"不可区分。
        #
        # N349：失败信号已在上面**算出来时**就填进 warnings 了
        # （见 `except SystemExit` 的 else 分支），不是在这里补 ——
        # 这里是"没算出来"的兜底，那时确实没有可报的原因。
        _pd_json = []
    else:
        # 并上 B 源（未 commit 的删除）—— A 对它恒盲。
        try:
            # N305：这里原先只过滤 `p in state["files"]`（本地基线），
            # 没补 `p in rstate`（远端仍在）—— 而文本侧补了（N285）。
            #
            # 后果是同一个命令、同一份状态，两侧给出**相反**结论：
            #   远端已删 b.txt、本地随后也删（未 commit）
            #   文本侧：什么都不报（正确）
            #   JSON 侧：count=1, paths=['b.txt']（多报）
            #
            # 而 pending_deletes 这个字段的注释原文写着"文本块会被忽略，
            # 机器可读字段不会 —— AI/CI 靠这个**可靠**拿到要删哪些"。
            # 唯一被承诺可靠的那一侧，恰好是没修的这一侧。
            # AI/CI 照它去删一个远端已经没有的文件，操作失败或误删同名新文件。
            #
            # 与文本侧同口径：files ∩ rstate。
            # N334：与文本侧**同源** —— 都从 _pd 派生，
            # 不再各调一次 _detect_deletions()（那是两次 git 子进程
            # 且判据不同，会在"远端树取不到"时给出相反结论）。
            _dd_j = [p for p in (_pd or []) if p not in (
                set(pending_deletions(state or {}, _lmap or {},
                                      _rstate_now) or []))]
            if _dd_j:
                _pd_json = sorted(set(_pd_json) | set(_dd_j))
        except BaseException:
            # M51b（A64，形态②）：A 源算出来了、**并 B 源时抛异常** ——
            # 此时 `_pd_json` **非空但不完整**（少了 B 源：未 commit 的
            # 删除，而 A 源对它恒盲）。
            #
            # 这一支比形态①更隐蔽：形态①至少是 `count=0`，一个"刚推完
            # 一堆文件却说没待删"的 0 还有点反常；形态②是 `count>0`
            # 且非空 —— **看起来和工作正常时一模一样**，只是悄悄少了一源。
            #
            # 而本字段的注释原文写着「AI/CI 靠这个**可靠**拿到要删哪些」。
            # 静默交付一份残缺清单，机器无从判断。置 degraded 让它不必靠猜。
            _pd_degraded = True

    wf = state.get("workflow") or DEFAULT_WORKFLOW
    payload = {
        "warnings": warnings,
        "workflow": wf,
        "baseline_health": _health,
        # 待删除清单。文本块会被忽略，机器可读字段不会 ——
        # 自动化场景（AI / CI）靠这个可靠拿到「哪些要删但没删」。
        "pending_deletes": {
            # M51（A55）：`count=0` **无法区分两种完全不同的情形** ——
            #   ① 确实没有待删除
            #   ② 远端树取不到 / 计算抛异常，`_pd_json` 根本没算出来
            # 实测两者 `{"count": 0, "paths": []}` **逐字相同**。
            # AI/CI 读到 count=0 会认为「没有要删的」，实际是「不知道」。
            # 本字段的注释原文写着「AI/CI 靠这个**可靠**拿到要删哪些」——
            # 唯一被承诺可靠的那一侧，不该要求消费者去 warnings 里做
            # 文案匹配才能还原这个事实。
            # degraded=True 时 count/paths **不代表**"没有"，代表"未知"。
            "degraded": _pd_degraded,
            "count": len(_pd_json or []),
            "paths": list(_pd_json or []),
            "auto_delete": not _deletes_disabled(),
            "action_required": (
                "本脚本不会删除（默认关闭，删除不可逆）。"
                "需人工核查后手动删除远端文件，"
                "或加 --delete 由本脚本处理。"
                if _deletes_disabled() else
                "删除已开启，下次推送会自动删除；确认无误再推。"),
        },
        "branch": BRANCH,
        "head": head_sha,
        "task_branch": state.get("task_branch"),
        "pr": state.get("pr_number"),
        # 配置一并给出：操作错了仓库是最难排查的问题，症状在远端。
        "config": dict(_CONFIG_INFO),
        "transport": probe_git_transport(),
        # K3（A5 / A23）：上面这些字段里，没有一个能回答「本次推送
        # 会不会被拦下」。而 --prune 那条路径展示的信息量明显更全
        # —— 两者不对称。
        #
        # 缺了它们的实际后果：--status 是排查"为什么推不上去"的
        # 第一入口，而 `conflicts`（未解决冲突）、`synced_commit`
        #（本地所基于的远端版本）、`local_head`（本地 HEAD）
        # 恰恰是三道 Guard 的判据输入。看不到就只能靠猜。
        #
        # 与 pr 字段同源，失败一律降级为 None 而不是崩掉。
        "conflicts": list(state.get("conflicts") or []),
        "synced_commit": state.get("synced_commit"),
        "local_head": _local_head_quiet(warnings),
    }
    # K3 附带：`_local_head()` 走 git()，而 git() 在非 0 退出码时
    # **默认打印一行告警**。在 --json 下那行会落在 JSON 之前，
    # 使整体不可解析（实测：json.loads 报 "Expecting value:
    # line 1 column 3"）—— 而它只是"这里没有 git 仓库"这种无害事实。
    #
    # 机器可读输出里，人读告警必须走 warnings 字段而不是 stdout。
    # 这里把 git 的告警收进 warnings，值本身降级为 None。
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        # N136 附带：本次 JSON 已由这里输出，main 出口的 Ui.flush()
        # 必须让路，否则输出两份、整体不可解析。
        _mark_json_emitted()
        return
    cfg = _CONFIG_INFO or {}
    cfg = _CONFIG_INFO or {}
    if cfg.get("repo"):
        src = cfg.get("repo_source", "")
        print(f"远端仓库：{cfg['repo']}"
              + (f"（来源：{src}）" if src else ""))
    # N66：root / root_source 被收集了却从不展示。
    # 而 cwd 不在 git 仓库内时 root 会回落到编译期默认值（指向另一个仓库）
    # —— 这是最难排查的问题：症状全在远端，本地一切正常。
    # 只打印 repo 不打印 root，用户无从发现自己在操作错误的目录。
    if cfg.get("root"):
        print(f"本地目录：{cfg['root']}"
              + (f"（来源：{cfg.get('root_source', '')}）"
                 if cfg.get("root_source") else ""))
        if cfg.get("root_hint"):
            print(f"  ! {cfg['root_hint']}")
    if cfg.get("state_path"):
        print(f"基线文件：{cfg['state_path']}"
              + (f"（来源：{cfg.get('state_source', '')}）"
                 if cfg.get("state_source") else ""))
        if cfg.get("state_hint"):
            print(f"  ! {cfg['state_hint']}")
    # 传输通道探测：这是「本脚本为什么存在」的前提，必须让人看见。
    # 放在 --status 而不是每次推送都打：探测要发一次网络请求，
    # 而它给的结论与单次推送无关（环境不变，结论就不变）。
    print(f"传输通道：{_transport_note()}")
    print(f"工作流：{'PR（每次新建 → 推送 → 自动合并 → 删分支）' if wf == 'pr' else '直推主干'}")
    print(f"主干 {BRANCH} = {str(head_sha)[:8]}")
    print(f"当前任务分支：{state.get('task_branch') or '（无，下次推送会新建）'}")
    print(f"当前 PR：{state.get('pr_number') or '（无）'}")
    # A7-四（K3 的**文本侧**）：JSON 侧早在 A5/A23 就把 conflicts /
    # synced_commit / local_head 收进了 payload，而文本侧一行都没有。
    #
    # 后果：--status 是排查「为什么推不上去」的第一入口，而它显示的
    # 恰好跳过了**所有会导致被拦的字段** —— 用户看到一片正常，推送被拦，
    # 拦截原因正是这里没显示的那些。
    #
    # 最能说明问题的是与 --prune 的对比：prune() 自称「只报告」，却给出
    # 分支名 / 合并状态 / PR 号 / 活动时间 / 建议动作一整套；--status
    # 本该是状态总览，信息量反而更少。
    #
    # 全部包在 try/except 里：任一项算不出来只是少显示一行，绝不能让
    # --status 这个**只读命令**崩掉（N121：remote_state_map 抛 SystemExit
    # 曾把 --status 整个搞挂）。as_json 已在上面 return，这里不会污染 JSON。
    try:
        _conf = [c for c in (state.get("conflicts") or [])
                 if isinstance(c, str)]
        if _conf:
            _more = f"，… 其余 {len(_conf) - 5} 个" if len(_conf) > 5 else ""
            print(f"未解决冲突：{len(_conf)} 个（{'、'.join(_conf[:5])}"
                  f"{_more}） —— 会**全局拦截**推送，先 --resolve")
        else:
            print("未解决冲突：0 个")
    except BaseException:
        pass
    try:
        _synced = state.get("synced_commit")
        if not _synced:
            print("本地副本：★ 未记录 synced_commit"
                  "（推送会被 P0-1 拦下，先 --pull）")
        elif _synced == head_sha:
            print(f"本地副本：已同步到 {str(_synced)[:8]}")
        else:
            print(f"本地副本：★ 落后于远端（基线记的是 {str(_synced)[:8]}，"
                  f"主干已是 {str(head_sha)[:8]}）"
                  f" —— 推送会被 P0-1 拦下，先 --pull")
    except BaseException:
        pass
    try:
        # 只在**记录过** local_head 时才查：缺字段那一支会打印
        # 「本版新增字段，检测暂不生效」并置全局 `_REWIND_NO_BASELINE_WARNED`
        # —— 那是推送路径的提示，--status 不该替它消耗掉一次性标志。
        if state.get("local_head"):
            _rw, _why = _local_head_rewound(state)
            if _rw and _why:
                print(f"  ⚠ {_why}")
    except BaseException:
        pass
    try:
        # 「落后于远端的文件」= 基线 sha ≠ 远端 sha（远端动过、本地没合）。
        #
        # N583（A152，低危）+ N507（第一百六十六轮，中危）：这里原先还写着
        # 「与 pull() 里 lagging 的定义同源」并引用了一个**行号**。
        # 两处都错，已一并删掉：
        #   · 行号引用必然漂移（那个行号现在是 `_pull_paths` 里「本地根本
        #     没有这个文件」那一支，与 lagging 无关）—— 引用行号等于埋一颗
        #     过期就反指别处的雷；
        #   · "同源"这个论断本身也是假的：那段原文写的是「一个远端已改、
        #     **本地还没合上**的文件」，明确含本地维度；本处的实现却只看
        #     `base sha != remote sha`，**两个维度都不在**。
        # 需要指称别处时请指函数名（可机械核对），不要指行号。
        #
        # 远端树取不到时如实说未知 —— 拿不到就说拿不到，
        # 不能像"待删除检测"早先那样用「没有」冒充「没算出来」。
        #
        # 下面两条实测假信息（N507）论证了本处判据为什么是错的：
        # 实测两条假信息：
        #   · 假阳：本地与远端逐字节相同、基线记的是旧版 → --status 报
        #     「落后 1 个」，同一份状态跑 --pull 却报「没有需要合并的」。
        #   · 假阴：远端只把 mode 从 100644 改成 100755、sha 一字未变 →
        #     --status 报「0 个」，而 FileBaselineGuard 明确拦下
        #     「远端 4163036e/100755 ≠ 基线 4163036e/100644」。
        # 后者尤其说不过去：同一个 PR 的 K2 刚给 validate_state 补了 mode
        # 取值校验（注释里逐字写着 mode 非法会让文件被永久判成他人改动），
        # K3 算「会不会被拦」时却把 mode 整维丢掉。
        #
        # 改成与 pull() 的 cand 同形：含本地（已合上就不算落后）、含 mode。
        # 本处纯展示、不参与任何判据，所以危害是**诊断方向错误**而非误拦。
        if _rs_ok:
            _lag = []
            for _p, _v in (state.get("files") or {}).items():
                _r = _rstate_now.get(_p)
                if _r is None:
                    continue
                _rt = (tuple(_r) if isinstance(_r, (tuple, list))
                       else ((_r.get("mode"), _r.get("sha"))
                             if isinstance(_r, dict) else None))
                if _rt is None:
                    continue
                _lm = (_lmap or {}).get(_p)
                if _lm is not None and tuple(_lm) == _rt:
                    continue      # 本地已合上 → 不落后
                _bm, _bs = ((_v.get("mode"), _v.get("sha"))
                            if isinstance(_v, dict) else (None, _v))
                if (_bm, _bs) != _rt:
                    _lag.append(_p)
            if _lag:
                print(f"落后于远端的文件：{len(_lag)} 个"
                      f"（{'、'.join(sorted(_lag)[:5])}"
                      f"{'，… 其余 %d 个' % (len(_lag) - 5) if len(_lag) > 5 else ''}）"
                      f" —— 先 --pull 合入")
            else:
                print("落后于远端的文件：0 个")
        else:
            print("落后于远端的文件：未知（远端树取不到）")
    except BaseException:
        pass
    print("\n常用：")
    print("  python3 push_api.py            # 新建分支 → 推 → 自动合并 → 删分支")
    print("  python3 push_api.py --hold     # 先不合并，攒几次改动")
    print("  python3 push_api.py --merge    # 合并 --hold 留下的 PR")
    print("  python3 push_api.py --prune    # 分支体检（只报告，不删）")
    print("  python3 push_api.py --scope    # 三方全景：谁动了哪些文件")


def show_scope(state, head_sha, rstate=None, as_json=False, only_mine=False):
    """--scope：一次性列出「这 N 个文件各自归谁」。

    与 --status 的分工：--status 报**脚本自身的状态**（基线、分支、PR、
    待删除），--scope 报**文件的归属**。两者分母不同，合在一起会让
    两件不同的事互相稀释 —— 但 --scope 的判据与 --status 严格同源
    （同一份 state["files"] / local_state_map / remote_state_map），
    不引入第二套口径。

    远端树**在函数内部自己取**，不走分派前那次 `remote_state_map`：
    那条前置路径下方跟着「远端 main = …」一类无条件 print（M86），
    它会落在 JSON **之前**让整体不可解析。--status 正是靠豁免那次取树
    才避开的，--scope 与它同构 —— 同理，--scope 也不进 _NEED_RSTATE。

    取不到远端树时**必须给合法 JSON 而不是静音**：与 M80 同一条判据 ——
    rc=0 加空输出等于「跑成了但没数据」，比报错更难发现。
    """
    if not state:
        # --mine 是**纯数据流**（要能被 `| while read f` 消费），
        # 任何人类文案都会混进管道被当成文件名。
        # 判不出归属就**一个都不输出**：空列表让调用方的循环自然结束，
        # 比输出一句说明然后让对方把说明当文件名去推要好。
        if only_mine:
            return
        if as_json:
            print(json.dumps({
                "ok": False, "baseline": False, "reason": "no_baseline",
                "hint": "先跑 --init-baseline 建立基线，再跑 --scope",
            }, ensure_ascii=False))
            _mark_json_emitted()
            return
        print("\n⚠️  没有基线记录，无法判定文件归属。")
        print("   请先跑 `python3 push_api.py --init-baseline`。")
        return

    if not rstate:
        try:
            rstate = remote_state_map(head_sha)
        except SystemExit as e:
            # SystemExit 不是 Exception 的子类（N121）—— 只写
            # except Exception 接不住，而 remote_state_map 在「远端 tree
            # 被截断 / 取不到」时正是抛它。接不住会让一个只读命令整个崩掉。
            if as_json:
                print(json.dumps({
                    "ok": False, "baseline": True,
                    "reason": "remote_tree_unavailable",
                    "detail": str(e)[:200],
                    "hint": "三方比对需要远端这一极；确认网络/token 后重试",
                }, ensure_ascii=False))
                _mark_json_emitted()
                return
            if only_mine:
                # 同上：管道里不能混文案。拿不到远端 = 判不出 MINE，
                # 空输出让调用方停在这里，而不是去推一份凭空猜的清单。
                return
            print(f"  ! 取远端文件树失败，无法做三方比对：{e}")
            return

    r = scope_classify(state, head_sha, rstate)

    # N469 / N470（第一百五十四轮）：「可直接推」这份清单**只算一次**，
    # --mine（纯数据流）与 --json（AI / CI 读归属的唯一可靠途径）共用它。
    # 分开各拼一句的结果就是 N385 只修了文本侧、JSON 侧照旧漏掉全部新增。
    #
    # N470：还要剔除脚本自造的运行时文件。它们天然满足 LOCAL_ONLY
    #（本地有、远端无），#278 把 LOCAL_ONLY 并入 --mine 之后就被**主动**
    # 列进推送清单了；而 --mine 是纯数据流、不能带警告，N450 那个守卫
    # 挂在 audit() 上、在 --mine 这条路径实测 **0 次调用** —— 于是远端
    # 明文副本（.remote）被静默推上远端。剔除后文本表仍会列出并标注，
    # 不是消失（用户要推可以点名推）。
    _pushable = list(r["MINE"]) + list(r["LOCAL_ONLY"])
    try:
        _selfmade = {_rel for _lb, _rel in _self_artifact_guard()}
        if _selfmade:
            _pushable = [p for p in _pushable if p not in _selfmade]
    except BaseException:
        # 探测失败 → 维持原清单（宁可多列，也不要静默漏掉用户的新文件）。
        _selfmade = set()

    # N462（第一百五十二轮，中危）：本地 HEAD 被回退时，同一份 state 在
    # 推送侧的结论是「推送会把主干静默回退」→ 拦下、退出码 2；而 --scope
    # 侧把同一个文件分类成 MINE 并建议「直接推送」—— 两条路对同一个文件
    # 给出相反的事实判断与相反的动作建议。回退时脚本判不出「哪些是你改
    # 的、哪些是你退回去的」（分类判据本身没错：本地 ≠ 基线、远端 == 基线，
    # 它区分不了"你改的"与"你退回去的"），所以 --mine **一个都不给**：
    # 空清单让 `| while read f` 的循环自然结束，原因写 stderr ——
    # stderr 不进管道，终端上仍然可见。
    _rewound_scope, _rewound_why = False, ""
    try:
        if state.get("local_head"):
            _rewound_scope, _rewound_why = _local_head_rewound(state)
    except BaseException:
        pass
    if _rewound_scope:
        _pushable = []

    if only_mine:
        # 只输出路径，一行一个，不含任何计数/表头/提示。
        # 用途：python3 push_api.py --scope --mine | while read f; do ... done
        #
        # 分类结果**能被消费**和**能被人看**是两件事。只给表格的话，
        # 想"逐个推送 MINE"就得先解析人类文案 —— 而解析文案正是本脚本
        # 反复批评的"纯数据消费者被迫去剥人类文案"。
        # MINE 的语义是"只有我改过、远端没动"，可以直接推。
        #
        # N385（第一百二十四轮，中危）：LOCAL_ONLY 同样是"可以直接推"
        # 的一类 —— SCOPE_HINTS 对它的建议动作原文就是「推送即新增」，
        # 而 USAGE 把 --mine 定位成"逐个推的清单"（可管道逐个推）。
        # 漏掉它，用户照它写出的循环
        #   --scope --mine | while read f; do push_api.py "$f"; done
        # 会**静默漏掉所有新建文件**（新增即静默不推），而表格里明明列着。
        #
        # 保持纯数据流：只多输出路径，不加任何文案。
        for p in _pushable:
            print(p)
        if _rewound_scope:
            # stdout 保持纯净（管道里一个字都没有），原因走 stderr：
            # 它不进 `| while read f`，终端上照样看得见。
            sys.stderr.write(
                "  ! --mine 未给出清单：本地 HEAD 已被回退（%s）\n"
                "    此时分不清哪些是你改的、哪些是你退回去的，推送会把主干"
                "静默回退 —— 先 `git reflog` 确认。\n" % _rewound_why)
        return
    if as_json:
        print(json.dumps({
            "ok": True, "baseline": True,
            "head": r["head"], "base_commit": r["base_commit"],
            "synced_commit": r["synced_commit"],
            "base_entries": r["base_entries"],
            "counts": {k: (len(r[k]) if isinstance(r[k], list) else r[k])
                       for k in ("MINE", "THEIRS", "BOTH", "LOCAL_ONLY",
                                 "REMOTE_ONLY", "UNKNOWN", "SAME")},
            # 清单本身也要给：--json 是 AI / CI 读归属的**唯一可靠途径**
            #（文本块会被忽略），只给计数等于让人再跑一次文本模式。
            "files": {k: r[k] for k in ("MINE", "THEIRS", "BOTH",
                                        "LOCAL_ONLY", "REMOTE_ONLY", "UNKNOWN")},
            # N469：`files` 把 MINE 与 LOCAL_ONLY **分开**给，而没有任何
            # 一处告诉消费者「这两类加起来才是可直接推的」。AI / CI 会照
            # 最像"我的改动"的那个字段 files["MINE"] 去拿，于是静默漏掉
            # 全部新增文件 —— 与 N385 完全同一条危害，只是消费者从人换成
            # 了 AI / CI（人不看表格还能靠常识补，AI 不会）。
            # 与 --mine 分支共用同一份 `_pushable`，不再各拼一句。
            "pushable": _pushable,
            # N462：回退时 pushable 为空，但**必须**说清为什么空，
            # 否则 AI / CI 会把 [] 读成"没有要推的"而不是"判不出来"。
            "rewound": bool(_rewound_scope),
            # N542 / N543：warnings 字段原先**只有 show_status 一个出口**。
            # 而 --scope --json 是 AI / CI 读归属的唯一可靠途径，它自己也
            # 会写状态（触发 _warn_mode 收起），收起后无人消费 → 被 main
            # 的 finally 清掉 → 安全警告（状态文件与远端明文副本世界可读）
            # 永久消失。而 A85 的注释刚论证过这类警告「静音等于让最需要
            # 知道的人看不到」—— CI 恰恰常跑在 virtiofs 上，最容易命中。
            # 同一个字段也承载 _pd_fail 那一路（待删除检测的中止原因）。
            "warnings": _drain_json_warnings(),
            "hints": dict(SCOPE_HINTS),
        }, ensure_ascii=False, indent=2, default=str))
        _mark_json_emitted()
        return

    # N406：本地曾拥有过的路径（基线 local_seen + files 里的历史记录）。
    # REMOTE_ONLY 与它的交集 = 用户自己删掉的，不是远端新增的。
    # 只用 local_seen，**不要**并入 state["files"] —— 后者是远端文件清单，
    # 把 README.md 这类"远端有、本地从未有过"的也算进去，标注就全成了 ①。
    # 实测：local_seen 里 keep.txt 有、README.md 无，正是要区分的那一条线。
    _seen = set(state.get("local_seen") or ())
    # N472：`lmap` 在本函数内**不存在**（无此形参、函数体内也无赋值），
    # 于是只要 moved_away 非空就 NameError。空字典时 dictcomp 不迭代、
    # 名字不被求值 → 不崩 —— 所以只有用户**真的搬过家**之后才崩，
    # 而那正是 N406 这条标注唯一要服务的场景。崩溃输出截断在归属表
    # 之前（只剩两行提示 + traceback），退出码 1 与"参数错误"同码。
    #
    # N473：判据必须与 pull() 侧（L6466）**逐字同源**——三条件。
    # 只补 lmap 不补 _moved_still_valid 的话，#281 修好的"记录失效"
    # 在 --scope 侧仍不成立，会打出「已判为搬到 n.txt（--pull 不会
    # 拉回旧路径）」而 --pull 恰恰会拉回来 —— 与 N398 修掉的那条
    # 反向矛盾重新出现，用户据此以为安全、跑完 --pull 撤销了重构。
    try:
        _lmap_scope = local_state_map()
    except Exception:
        _lmap_scope = {}
    # N593：`_ma_all` 是**未过滤**的搬家记录 —— 上面那一支只标「仍成立」
    # 的，失效的那些此前会掉进「本地曾有过（你删的）」，把改名说成删除。
    _ma_all = dict(state.get("moved_away") or {})
    _ma_scope = {o: n for o, n in _ma_all.items()
                 if n in _lmap_scope and o not in _lmap_scope
                 and _moved_still_valid(rstate, o, _lmap_scope, n,
                              state.get("files"))}
    print(f"主干 {BRANCH} = {str(head_sha)[:8]}")
    print(f"基线基准 = {str(r['base_commit'])[:8]}"
          f"（共记 {r['base_entries']} 条）")
    print()
    for k in ("MINE", "THEIRS", "BOTH", "LOCAL_ONLY", "REMOTE_ONLY", "UNKNOWN"):
        v = r[k]
        # N399（第一百二十七轮，中危）：UNKNOWN 的 hint 是**静态常量**，
        # 不能接上"此刻有没有可信基点"这份判断。而 init_baseline 自己
        # 刚打印过（约 20 行、分两种情况）"此时不要跑 --pull"。
        # 同一份文件里两处对同一件事的陈述相反 —— 与 N371 同族。
        _hint = SCOPE_HINTS[k]
        if k == "UNKNOWN" and v:
            _hint = _unknown_hint(state)
        if k == "MINE" and v and _rewound_scope:
            # N462：回退时不能再写「只有你改过 → 直接推送」——推送侧
            # 对同一文件的结论恰恰是「会被拦下，退出码 2」。
            _hint = ("⚠ 本地 HEAD 已回退，先 `git reflog` 确认哪些改动还在"
                     "（此时 MINE 里混着被退回的旧版本，直接推会回退主干）")
        print("%-12s %4d   %s" % (k, len(v), _hint))
        for p in v[:8]:
            # N406：REMOTE_ONLY 里标出「本地曾有过」的那些 —— 它们是用户
            # **自己刚删/移走**的，--pull 会把它们拉回来（撤销重构）。
            # 只给文字判据等于让用户手工做；脚本有这份信息就该打出来。
            tag = ""
            if k == "REMOTE_ONLY" and p in (_ma_scope or {}):
                # N398：moved_away 里的旧路径要**单独标注**，否则本表
                # hint 说"→ --pull 补齐"而 --pull 恰恰跳过它，
                # 同一次输出里 REMOTE_ONLY 与 LOCAL_ONLY 成对出现时
                # hint 与行为相反。
                #
                # ⚠️ 本注释原先写「--pull **必然**跳过」——那是 N373
                # 之前的形态。N373 之后 `--pull` 会先用
                # `_moved_still_valid` 复核记录是否仍成立：远端旧路径上
                # 已换成别的内容时记录失效，--pull **会拉回**旧路径
                # （A119 §2.3 实证）。
                # 与本注释同一行的判据已经跟着改了（上面 `_ma_scope`
                # 三条件与 pull 侧逐字同源），所以这里标"不会拉回"
                # 只在记录**仍成立**时才成立 —— 不是无条件必然。
                # 注释停在修复前 = 与代码不符（同 N63 / N100 / A108 那族）。
                tag = (f"   ← 已判为搬到 {(_ma_scope or {})[p]}"
                       f"（记录仍成立时 --pull 不会拉回旧路径）")
            elif k == "REMOTE_ONLY" and p in _ma_all:
                # N593（第一百九十三轮，中危）：记录**真的**失效时，不能
                # 掉进下面「本地曾有过（你删的）」那一支。
                #
                # 用户做的是**改名**，不是删除 —— b.txt 好端端在本地。
                # 而「--pull 会拉回来」这句在此刻字面上是真的（记录失效后
                # pull 不再跳过旧路径），于是形成一个自洽但整体错误的闭环：
                #   判据失效 → 标注降级成「你删的」→ hint 说「→ --pull 补齐」
                #   → 照做就把改名撤销了，而 --pull 全程不说它撤销了什么。
                #
                # 与 N592 是同一条链的两半：N592 修的是「什么时候该失效」，
                # 本处修的是「真失效之后该怎么说」—— 只修前者，N373 原本
                # 要防的场景（他人在远端旧路径上放了别的内容）下本标注
                # 依旧把改名说成删除。
                tag = (f"   ← 曾判为搬到 {_ma_all[p]}，但该记录已失效"
                       f"（远端旧路径内容已变，或新路径已不在本地）"
                       f"—— --pull **会**拉回旧路径，等于撤销改名")
            elif k == "REMOTE_ONLY" and p in _seen:
                tag = "   ← 本地曾有过（你删的；--pull 会拉回来）"
            print("             %s%s" % (p, tag))
        if len(v) > 8:
            print("             … 另 %d 个" % (len(v) - 8))
    print("%-12s %4d" % ("SAME", r["SAME"]))
    # 未同步提示必须**带数字出现在这里**：读表的人下一步就是决定推不推，
    # 而 THEIRS 非空时推送会被第一层拦下 —— 不写这句，用户会以为表格
    # 说「MINE 3 个」就意味着「可以直接推 3 个」。
    if r["THEIRS"] or r["BOTH"]:
        print(f"\n  ! 还有 {len(r['THEIRS'])} 个 THEIRS + {len(r['BOTH'])} 个 BOTH "
              f"未并入本地，推送会被拦下：python3 push_api.py --pull")


def _unknown_hint(state):
    """N399：UNKNOWN 的建议动作，按「此刻有没有可信基点」分情况。

    无可信基点（`synced_commit` 为空）时不能建议 `--pull` ——
    init_baseline 对同一件事有约 20 行相反的说明。与 N404（第一层
    Verdict.remedy 分情况）同源：同一份判断，两处出口必须一致。
    """
    if (state or {}).get("synced_commit"):
        return (SCOPE_HINTS["UNKNOWN"]
                + "；若 --pull 判定基点不可信，请人工确定共同祖先"
                  "（git merge-file）或改用点名推送")
    return ("基线缺失或已漂移 → 无法判定归属，不推；"
            "⚠️ 此刻**没有可信基点**，不要跑 --pull（它会退回用本地 git HEAD"
            "当合并基点，可能让已 commit 的改动静默消失，且冲突未必是真冲突）。"
            "先 --mark-synced 或 --reset-baseline 建立基点，"
            "或人工确定共同祖先（git merge-file）/ 改用点名推送")


def _rstate_sha(rstate, rel):
    """取远端树里 rel 的 sha。兼容 (mode, sha) 元组与 dict 两种形态。

    N592 收尾：这里原先是一份与 `_entry_sha` **逐行相同**的实现
    （dict / tuple / 裸值三档各写一遍）。与本批反复记的「两份实现迟早
    漂成两套口径」同源 —— 事实正是这么发生的：`_entry_sha` 漏掉元组
    那一档时，这里是对的，于是**同一个条目在两个出口上给出不同结果**
    （--scope 侧真搬家被判失效、标注消失）。收敛成一份，取 sha 只留一个
    出口。（形态判定仍在这里，是为了保持原有语义：裸值一律 None。）
    """
    v = (rstate or {}).get(rel)
    if isinstance(v, dict):
        return _entry_sha(v)
    if isinstance(v, (tuple, list)) and len(v) >= 2:
        return _entry_sha(v)
    return None


def _moved_still_valid(rstate, o, lmap, n, base=None):
    """N373（第一百一十八轮）：搬家记录 `o → n` 是否**仍然成立**。

    原判据只看「新路径还在不在」（`n in lmap`），于是旧路径被删掉之后，
    只要新路径一直在，记录**永久保留**。若日后远端（或他人）在**同一个
    旧路径**上重新放了文件，pull 仍命中 `rel in _moved_away` → 跳过落盘
    → 那个文件永远拉不回来（数据缺失）。

    这一问的**准确表述**是：「远端旧路径上那份，还是不是当初搬走的那份」。
    要问的是**远端变没变**，所以基准必须是「当初搬走的那份」，
    而「当初搬走的那份」正是记录建立时基线里记的 `o`（N592 实测：
    改完名推一次之后基线里 `o` 的条目**仍在**，数据是现成的，无需新增存储）。

    ### N592（第一百九十三轮，中危）：基准取错了对象

    上一版比的是 `远端旧路径 sha` vs **`本地新路径当前 sha`**：

        rs = 远端旧路径 sha
        ls = 本地新路径**当前** sha
        return rs == ls

    这两者只在「用户此后再没碰过新文件」时才相等。而「改名 → 推送 →
    继续编辑新文件」是改名的**正常后续**：

        改名 a.txt → b.txt 推送（基线 a.txt = v1，远端 a.txt = v1）
        → 用户编辑 b.txt 成 v2 → 再推送
        → 现状判据：rs(v1) == ls(v2)? → False → **判失效**
        → 下一次 --pull 把 a.txt 原样拉回本地（内容 v1），**改名被撤销**

    而远端 a.txt 从头到尾没人动过 —— 按它自己的定义（「远端那份已经不是
    当初搬走的那份 → 失效」）应当判**仍成立**。变的是本地 b.txt（用户
    自己改的），不是远端。

    判据必须与 `detect_moves` 同源：**内容完全相同**才算搬家 —— 同源指的
    是与**当初那份**比，不是与本地当前内容比。

    ⚠️ 不能只用 `o in rstate` 判失效 —— 远端旧路径**一直存在**是常态
    （本地搬走但没删远端），那样会让搬家判定整体失效，本地出现两份。

    ### 拿不到「当初那份」时怎么办（退回旧判据，不是维持原判）

    base 为 None，或 base 里**没有** `o` 这一条 —— 都无从回答「远端变没变」。
    此时必须退回 N373 的原判据（rs vs 本地新路径当前内容），**不能** return True。

    理由：base 里没有 `o`，恰恰是 N373 原本要防的那个场景的常态 ——
    他人在远端旧路径上放了别的内容，而这条路径此前已从基线摘掉
    （direct 工作流下删除生效即摘，见 `_refresh_main_baseline` / push 侧
    的 `state["files"].pop`）。此时若 return True（维持原判=仍算搬家），
    pull 就会永久跳过这条旧路径，**远端那份永远拉不回来**（N373 原话：
    数据缺失）。退回旧判据则能正确判失效。

    实测（本轮收尾）：第一版写成 `if bs is None: return True`，
    `测试/test_moves.py::n373_moved_away_expires` 立刻转红 —— 用例里
    base 只有 new.txt、没有 old.txt，正是上面这个场景。

    而 N592 那条误判**不会**因此复活：误判的前提是「改名 → 推送 → 用户
    编辑新文件」，此刻旧路径要么仍在远端（删除功能默认关闭 / PR 未合并）
    → 基线条目仍在、走新判据；要么已从远端删掉 → `rs is None` → 上面
    第一行就返回 True，根本到不了比对的环节。
    """
    rs = _rstate_sha(rstate, o)
    if rs is None:
        # 旧路径不在远端：无从比对，维持原判（不引入新猜测）
        return True
    if n not in lmap:
        return False
    if base is not None:
        # 与「当初搬走的那份」比 —— 问的是远端变没变
        b = (base or {}).get(o)
        bs = _entry_sha(b)
        if bs is not None:
            return rs == bs
        # 基线条目**缺失**时不能「维持原判（True）」—— 那正是 N373 原本
        # 要防的场景被吞掉的方式。见下面「基线条目缺失 → 退回旧判据」。
    ln = lmap.get(n)
    if isinstance(ln, dict):
        ls = ln.get("sha")
    elif isinstance(ln, (tuple, list)) and len(ln) >= 2:
        ls = ln[1]
    else:
        ls = None
    if ls is None:
        return True
    return rs == ls


def _baseline_stale(cur, mode, sha):
    """M124（A114）：基线条目 `cur` 是否**还落后于** `(mode, sha)`。

    返回 True 表示应当把这一条刷新成 `(mode, sha)`；False 表示已一致、
    可跳过。判据必须是 **(mode, sha) 二元组相等**，不能只比 sha。

    只比 sha 的后果（本报告实测）：远端把某个文件从 100644 改成 100755
    而内容一字未变时，sha 相同 → 恒判"已一致" → 永远跳过。而外层
    `lmap == rstate` 此刻**已经成立**（本地也已 chmod 跟上），于是这条
    错误的 mode 此后**没有任何出口**会被修正：
      · 刷新是唯一出口，而刷新判据说"不用刷"
      · 第一层看 `远端 == 基线`…也不成立，mode 不同
      · 结果：该文件每次都被算作"有差异"，又每次都"刷新不掉"

    这与 N422 已修的那一处是**同一份事实**：N422 修的是「远端变过吗」
    这一问（mark_synced），漏的是「基线要不要刷新」这一问的各处。
    抽成同一份判据，不再靠"谁想起来补谁"。

    ⚠️ **不要在注释里写死"共 N 处"**（N583 / A152）：
    这段 docstring 原先也写死了一个**计数**（声称刷新点一共 5 处），
    而 `_refresh_main_baseline` 自己的注释（N34）又称自己是"第 5 个
    基线刷新点" —— 两边各数各的，合起来是**六处**，第六处无人认领，
    也就没被收敛。
    而漏掉的恰恰是唯一掌管 `synced_commit` 的那一处（N630 已修）。
    计数写死在注释里必然漂移，且漂移后**不会报错** —— 只有读者照着
    核对才发现"数全了"是假的。要指称就指函数名（可机械 grep），
    不要指数目。

    `cur` 可能是 None / 裸 sha 字符串（v1 形态）/ dict，三种都要处理：
    非 dict 一律视为需要刷新（裸 sha 没有 mode，必然不同）。
    """
    if not isinstance(cur, dict):
        return True
    return (cur.get("mode"), cur.get("sha")) != (mode, sha)


def _prune_moved_away(ma, lmap, rstate, base=None):
    """N474（第一百五十五轮）：无条件清掉**已失效**的搬家记录。

    原实现挂在 `if _mv:` **内部** —— 于是只有本次检测到新搬家时，存量
    记录才会被过滤一遍。而 `_mv` 为空才是常态：搬完家推过一次之后，
    新路径就进了基线，`detect_moves` 的 added 集合此后恒空（该判据自己
    的注释就写着这句）。

    时序上必然的组合：
      搬家当下推送（记录写入且有效）
        → 远端旧路径内容变了（记录失效）
        → 此后每次 `_mv` 都为空 → 失效记录**永久留在 state 里**

    后果与 N373 原文批评的「记录只增不减」是同一件事，#281 只堵了
    「新增时」那一半。

    ⚠️ 只清**失效**的，不清「新路径不在本地」之外的 —— 判据与
    `_moved_still_valid` 一致，不引入第二套口径。

    `base` 必须传（N592）：`_moved_still_valid` 的基准是「当初搬走的那份」，
    不传就退回「本地新路径当前内容」—— 而那正是 N592 那条误判的来源：
    用户编辑过新文件 → 判失效 → 本函数把**仍然成立**的记录清掉，
    于是下一次 --pull 把旧路径拉回来、改名被撤销。清理与判据必须同源。
    """
    return {o: n for o, n in (ma or {}).items()
            if n in lmap
            and _moved_still_valid(rstate, o, lmap, n, base)}


def _prune_stale_tasks(state, remote_names, truncated=False):
    """摘掉 tasks 里「远端已无此分支」的条目。

    tasks[branch] 只在 merge / abandon / new-task 时清理。推送失败时
    条目不清理，而 prune 的分类只覆盖**远端还存在的**分支，于是
    「本地记着、远端早已没了」的条目会永久累积：
      · 状态文件缓慢膨胀
      · --prune 输出里出现早已不存在的分支（用户会去找一个不存在的东西）
    分支都没了，本地记录就只剩误导作用。

    只动 state，不碰远端 —— 与「--prune 不删除任何东西」不冲突。
    """
    tasks = state.get("tasks") or {}
    # N70：列表被截断时不能当完整依据。
    # 「不在列表里」在这种情形下有两种解释：远端真没了 / 只是没被列出。
    # 按前者处理会删掉 tasks[branch].head 与 .pushed ——
    # 前者是 abandon_task「分支上有未记录改动则拒绝删除」的依据，
    # 后者是第四层校验的 mine 集合。删掉它们等于拆掉两道防护。
    if truncated:
        print("\n  ! 远端分支列表可能被截断，本次不清理本地任务记录"
              "（避免误删仍在使用的判据依据）。")
        return
    stale = [n for n in tasks if n not in remote_names]
    if not stale:
        return
    for n in stale:
        tasks.pop(n, None)
    # 当前任务分支若已被摘除，同步清掉，避免指向一条空记录
    #
    # N93：原先只清 task_branch，不清 pr_number。而另外 4 处清理点
    # （merge / abandon / close_pr / new_task）都是两个一起清 ——
    # 于是这里留下一个指向已删 PR 的悬空号：
    # 下次 --merge 拿它去查，查到的是一条不存在或早已无关的 PR，
    # 而用户看到"当前 PR：42"会以为真有一个。
    if state.get("task_branch") in stale:
        state["task_branch"] = None
        state["pr_number"] = None
    save_state(state)
    print(f"\n[已清理] {len(stale)} 条本地任务记录（远端已无此分支）："
          f" {sorted(stale)[:5]}{' …' if len(stale) > 5 else ''}")
    print("   仅清理本地记录，未改动远端。")


def prune(state, grace_days=MERGED_GRACE_DAYS):
    """分支体检：**只报告，绝不删除**。

    判据用「合并状态 + 活动状态」，不用「N 天没用」——
    一个开发到一半的 WIP 分支，恰恰就是「开着、没合并、可能好几天没动」，
    按时间自动删会最先误杀它。

    已合并但分支仍在的，只有超过 grace_days 天才报告：
    正常流程合并完立刻就删了，刚合并还没删的多半是流程还在跑，
    立刻报告会让两个人互相反复刷同一条。
    """
    # 必须放在 docstring **之后**：上一版这条语句写在 docstring 前面，
    # 于是那段长注释不再是 docstring（prune.__doc__ is None），
    # 被 Python 当成一个无用的字符串字面量丢掉 ——
    # 「只报告不删」这份核心契约因此 help() 看不到、文档工具抓不到。
    _report_conflict_artifacts()
    # 排除法，不用前缀白名单。
    #
    # 原来只认 task/ 前缀：pr/ fix/ feature/ 等前缀的分支**静默漏报**，
    # 而 --prune 的全部价值就在于「不漏」——它报出的「很干净」会被当成
    # 真的干净。实测：一次推送用的 pr/backfill-round2 根本不在视野内。
    #
    # 改成排除主干与受保护分支：只要不是这些，一律纳入体检。
    # 宁可多报（用户能自己判断），不可漏报（他以为没事）。
    protected = {BRANCH} | set(PROTECTED_BRANCHES)
    # N302：用 _list_branches_ex() —— truncated 只反映**分支列表**，
    # 不会被随后的 PR 列表触顶污染（PR 数远超分支数是常态）。
    _all, _trunc = _list_branches_ex()
    # N302：保存下来供本函数后段复用 —— 后段在 PR 列表之后，
    # 那时再读全局标志会拿到 PR 的截断结果。
    del _BRANCH_TRUNC[:]
    _BRANCH_TRUNC.append(_trunc)
    mine = [b for b in _all if (b.get("name") or "") not in protected]
    excluded = ", ".join(sorted(protected))
    # N255：早退路径也要拿到真实的截断标志（原先硬编码 False）。
    if not mine:
        print(f"\n没有受检分支（已排除 {excluded}），很干净。")
        # 仍要清理僵尸 tasks 条目：一个 task/* 分支都没有，
        # 恰恰说明 tasks 里的记录**全**是过期的 —— 这里 return 掉
        # 的话它们永远清不掉，正是 P3-5 要治的累积问题。
        # N255：这里**硬编码** truncated=False，而正常路径传的是
        # `_trunc`（实际探测到的截断标志）。
        # truncated 的唯一作用就是"列表被截断时不按完整依据清理 tasks"
        #（N70：误删会拆掉 abandon_task 与第四层的判据依据）。
        # 早退路径写死 False = 声称"列表完整"。
        #
        # 但也不能简单地"早退就全不清理"：mine 为空有两种成因，
        # 必须分开：
        #   a) 远端确实只有受保护分支（_all 非空、未截断）
        #      → 可以清理，但传**全部**分支名（含受保护，见 N224），
        #        不能传空集合
        #   b) 列表被截断 / 一个分支都没取回（请求失败？）
        #      → 不清理
        #
        # 传空集合会把 tasks 全部判成"远端已无此分支"——
        # 在 (b) 的情形下等于拆掉两道防护。
        if _trunc or not _all:
            print("\n  ! 远端分支列表不完整（截断或未取回），"
                  "本次不清理本地任务记录。")
            return
        _prune_stale_tasks(state, {b.get("name") for b in _all},
                           truncated=_trunc)
        # M22（A19，行号经 A24 更正）：原先这里有两个连续的
        # `return` —— 第二个永不执行（死代码）。
        # 它是 N255 修 prune 早退路径时留下的：补了新分支、旧行没删。
        # 留着会让人以为清理完还有一步，也会误导行号定位。
        return

    merged_stale, merged_recent, opening, orphan, closed = [], [], [], [], []
    skewed = []                      # 时间戳落在「未来」→ 本地时钟慢
    merged_unknown = []              # N96：合并时间解析不出 → 必须报
    # 一次建索引代替循环内逐个 find_pr（N+1 → 1 次请求）。
    pr_index = _pr_index("all")
    for b in mine:
        name, last = b.get("name"), _branch_date(b)
        pr = pr_index.get(name)
        if pr is None:
            orphan.append((name, last))
        elif pr.get("merged_at") or pr.get("merged"):
            d = _days_since(pr.get("merged_at"))
            # 负值 = 本地时钟慢于 GitHub 服务器（时间戳还在「未来」）。
            #
            # 报告建议 `max(0.0, d)` 兜住，但那治标不治本：
            # max(0,d) 之后 d=0，`0 >= grace_days` 依然是 False，
            # 陈旧分支照样不报 —— 只是把「负值」这个信号抹掉了，
            # 让它看起来像「刚合并、无需处理」，反而更静默。
            #
            # 正确做法是**让时钟不一致显形**：提示用户，并按最保守的
            # 方式处理（时钟不可信时，无法判断新旧，不该假装安全）。
            if d is not None and d < 0:
                skewed.append((name, pr["number"], d))
            # N415（第一百三十三轮，低危）：`d < 0` 的分支原本**同时**
            # 落进 skewed（⚠ 时钟不一致）与 merged_recent（无需处理）——
            # 同一份输出里一个分支既被标"分类可能不准"，又被计入
            # "刚合并、无需处理"。与此处注释直接矛盾：「时钟不可信时
            # 无法判断新旧，不该假装安全」。
            # 归 merged_unknown（无法判断）才是那个"最保守的方式"。
            if d is None or d < 0:
                # N96：d 解析不出（merged_at 格式异常 / 时钟偏移到无法计算）
                # 原本落到 merged_recent → **静默不报**。
                # 而 prune 的全部价值就在"不漏报"：用户拿"体检很干净"
                # 当真，这个文件就永远不会被看到。
                # 归入单独一类并明确报出「无法判断」。
                merged_unknown.append((name, pr["number"], d))
            elif d >= grace_days:
                merged_stale.append((name, pr["number"], d))
            else:
                merged_recent.append((name, pr["number"], d))
        elif pr.get("state") == "closed":
            closed.append((name, pr["number"], last))
        else:
            opening.append((name, pr["number"], pr.get("updated_at") or last))

    print(f"\n受检分支 {len(mine)} 个（已排除 {excluded}；只报告，不删除任何东西）：")

    if merged_stale:
        print(f"\n[应当清理] 已合并超过 {grace_days} 天，分支却还在"
              f"（正常流程早该删了）：")
        for name, n, d in merged_stale:
            print(f"   · {name}   PR #{n}   已合并 {d:.0f} 天")
            print(f"     python3 push_api.py --delete-branch {name}")
    if merged_unknown:
        # N96：无法判断合并时间 —— 不能归进"无需处理"。
        # prune 是体检工具，"报不出来"和"确实没问题"必须分开。
        print("\n[无法判断] 已合并但合并时间解析不出，"
              "无法判断是否超过保留期：")
        for name, n, d in merged_unknown:
            print(f"   · {name}   PR #{n}"
                  + ("   （合并时间落在「未来」，本机时钟慢于服务端）"
                     if d is not None and d < 0 else ""))
            print("     请到网页确认合并日期，再决定是否清理。")
    if skewed:
        print("\n[⚠ 时钟不一致] 以下分支的合并时间落在**未来**，"
              "说明本机时钟比 GitHub 服务器慢：")
        for name, n, d in skewed:
            print(f"   · {name}   PR #{n}   时间差 {abs(d):.1f} 天")
        print("   时钟不可信时无法判断分支新旧，上面的分类可能不准。")
        print("   建议校准本机时间（ntpdate / 系统时间设置）后重新 --prune。")
    if merged_recent:
        print(f"\n[无需处理] 刚合并、分支待删（{grace_days} 天内，属正常流程）："
              f" {len(merged_recent)} 个")
    if opening:
        print("\n[保留] PR 仍开启 —— 可能是进行中的工作，不删：")
        for name, n, ts in opening:
            d = _days_since(ts)
            extra = f"，{d:.0f} 天无更新" if d is not None else ""
            # tasks[branch]["last_push"] 以前**只写不读**（死字段）。
            # 维护者会误以为它参与判定，而实际上没有 —— 这种隐性误导
            # 比缺少字段更糟。这里让它真正派上用场：PR 的 updated_at
            # 会因为评论等无关活动而变新，而 last_push 才反映
            # 「最后一次真的推了内容」，判断 WIP 是否还在推进更准。
            pushed = ((state.get("tasks") or {}).get(name) or {}).get("last_push")
            if pushed:
                pd = _days_since(pushed)
                extra += (f"（最后推送 {pushed}"
                          + (f"，{pd:.0f} 天前" if pd is not None else "") + "）")
            print(f"   · {name}   PR #{n}   最后活动 {ts or '-'}{extra}")
        print("   确认不做了：python3 push_api.py --branch <名字> --close-pr")
    if closed:
        print("\n[按需删] PR 已关闭且未合并（改动没进主干，但分支还在）：")
        for name, n, ts in closed:
            print(f"   · {name}   PR #{n}   最后提交 {ts or '-'}")
            print(f"     python3 push_api.py --delete-branch {name}")
    if orphan:
        # tasks 里还在跟踪的分支**不能**当孤儿处理。
        #
        # 它们多半是「分支已推上内容、但开 PR 失败」留下的（见 --push 时
        # 的 P1-8 提示）。此刻分支上装着用户刚推的东西，而 --prune 恰恰
        # 会把它判成孤儿并给出 --delete-branch —— 工具引导用户删掉自己
        # 的改动，与推送时那句「不要 --delete-branch」自相矛盾。
        tracked = set((state.get("tasks") or {}).keys())
        orphan_free = [(n, t) for n, t in orphan if n not in tracked]
        orphan_held = [(n, t) for n, t in orphan if n in tracked]

        if orphan_held:
            print("\n[先确认] 无 PR、但本地还在跟踪的分支（可能装着刚推的改动）：")
            for name, ts in orphan_held:
                print(f"   · {name}   最后提交 {ts or '-'}")
                print("     多半是开 PR 失败留下的。先重试：")
                print(f"       python3 push_api.py --branch {name} -m \"<消息>\"")
                print("     ⚠ 确认分支上没有你的改动后，才可 --delete-branch。")

        if orphan_free:
            print("\n[按需删] 没有任何 PR 的孤儿分支：")
            for name, ts in orphan_free:
                print(f"   · {name}   最后提交 {ts or '-'}")
                print(f"     python3 push_api.py --delete-branch {name}")
    if not (merged_stale or opening or closed or orphan):
        print("   全部无需处理")

    # N70：_paginate 的 cap 触顶时会打印警告、明确**不假装结果完整**，
    # 而这里此前把它直接喂给了会删除的消费者。
    # _prune_stale_tasks 的判据是「不在列表里就删」，列表若被截断，
    # 真实存在但排在后面的分支，其 tasks 条目会被误删 ——
    # 连带失效的是 tasks[branch].head（abandon_task 的"有未记录改动"判据）
    # 与 .pushed（第四层校验的 mine 集合）。
    # 截断时改为不清理（宁可保留陈旧条目，也不能删掉还需要的依据）。
    # N302：同 7034 那处 —— 取**分支列表自己的**截断标志。
    # 这里在 PR 列表之后，用全局单标志会直接读到 PR 的截断结果。
    # 分支列表在前文已由 _list_branches_ex() 取过，复用它的标志。
    _trunc = _BRANCH_TRUNC[0] if _BRANCH_TRUNC else False
    # N224：`mine` 是「**该不该进体检报告**」的集合（已排除受保护分支），
    # 而 _prune_stale_tasks 要的是「**远端实际存在哪些分支**」。
    # 两者语义完全不同却共用一份集合 ——
    # 受保护分支不在 mine 里，于是它在 tasks 里的记录被当成
    # 「远端已无此分支」而摘掉。
    #
    # 而 tasks[branch].head 是 abandon_task「分支上有未记录改动则拒绝删除」
    # 的依据，.pushed 是第四层校验的 mine 集合 ——
    # 摘掉等于拆掉两道防护（N70 明确警告过这个后果）。
    #
    # 传**全部**远端分支名，不排除受保护分支。
    _prune_stale_tasks(state, {b.get("name") for b in _all},
                       truncated=_trunc)
    print("\n（删除一律走 --delete-branch 并逐个确认；--prune 自己不删任何东西）")


# ---------------------------------------------------------------- 预览

def _remote_blob_lines(sha, size_hint=None):
    """取远端 blob 的文本行；拿不到、二进制或超大返回 None。

    注：GitHub 对超过 1MB 的 blob 在 JSON 响应里不返回 content，此时降级。

    N622 之后**生产零调用**：预览改成"拉字节一次、行由字节派生"
    （`_remote_blob_bytes` + `_lines_from_blob_bytes`）。保留它不是死代码
    —— 与 M27（`Ui.ok` 生产零调用但 `test_ui.py` 依赖）同一形态：
    `test_r59_65_66_67` 用它验证 N236 的降级路径，`test_a4_h1_h2_h3`
    原先靠打桩它构造预览对照（已改为打桩 `_remote_blob_bytes`）。
    登记用途，避免下一个人按"零调用即死代码"把它删掉。
    """
    if size_hint is not None and size_hint > MAX_PREVIEW_BYTES:
        return None
    # N236：`api()` 不在 try 内 —— 网络失败 / 限流 / 404 会抛 SystemExit，
    # 一路冒到 preview() 外面，**整次推送中断**。
    # 而预览只是"顺便看看"，它崩掉不该带走主流程。
    # 只有解码那段被包了，那是最不可能失败的一步。
    try:
        data = api("GET", f"/git/blobs/{sha}")
    except BaseException:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("encoding") != "base64" or not data.get("content"):
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", "replace").splitlines()
    except Exception:
        return None


#: 两边字节都读到了、却找不出差异时的兜底措辞（N281）。
#:
#: 抽成常量是因为调用点要**分辨**它：`仅{why}` 这个句式对兜底不成立
#: —— 兜底的意思是「说不准」，加「仅」就把不确定说成了确定
#: （M202，A169，低）。靠字符串比较分辨不如靠常量。
_ZERO_DIFF_UNKNOWN = "字节存在差异（行尾/编码之外的形态，如 BOM 或 NUL）"


def _lines_from_blob_bytes(rb):
    """由**已取到的**远端原始字节派生文本行（N622）。

    原来 `preview()` 先调 `_remote_blob_lines()` 拉一次、判零差异时再调
    `_remote_blob_bytes()` 拉第二次 —— 同一个 sha、同一个端点，零差异
    文件必发两次 GET /git/blobs。行本来就是字节解码来的，没必要拉两遍。
    """
    if rb is None:
        return None
    try:
        return rb.decode("utf-8", "replace").splitlines()
    except Exception:
        return None


def _local_blob_bytes(rel, mode=None):
    """本地文件的**原始字节**（N620：判「纯 mode 变更」必须用字节）。

    `old == new` 比的是 splitlines() 之后的行 —— 行尾归一化把「缺末尾
    换行」这类**字节确实不同**的情况也判成相等。N281 那条
    「纯 chmod 不该进归因」的判据因此会连带吞掉真正的字节差异告知。

    symlink 的内容是它指向的目标路径字符串（与 `_local_lines` 同源）。
    """
    full = os.path.join(ROOT, rel)
    if mode == "120000":
        try:
            return os.readlink(full).encode("utf-8", "surrogateescape")
        except OSError:
            return None
    try:
        if os.path.getsize(full) > MAX_PREVIEW_BYTES:
            return None
        with open(full, "rb") as f:
            return f.read()
    except OSError:
        return None


def _zero_diff_reason(rel, remote_bytes=None):
    """零文本差异但字节确实不同时，说明是哪一类差异。

    N237：预览靠 splitlines() 比对，行尾与编码差异被归一化掉了。
    这里按原始字节判断，给出具体成因。

    N286（第八十三轮，低危）：原本还有个 ``mode`` 形参，函数体**零引用**
    —— #66 加 remote_bytes 时没带上它，mode 变更也早由调用点的
    ``if mode_changed and old == new: continue`` 单独处理了。
    死形参会诱导人以为"这里会区分 symlink / 可执行位"，于是把该做的
    判据以为已经做了。已删（调用点同步去掉 lmode）。

    N281：差异可能只在**远端**一侧，而原判据全落在本地字节上。
    实测三种最常见场景（远端内容固定，只改本地）：

      · 纯 chmod +x（内容一字节未变）→ 报"行尾或编码"（新增误报）
      · 远端 CRLF、本地 LF          → 落兜底
      · 远端缺末尾换行、本地有       → 落兜底

    只有差异落在本地时才归因准确。而"行尾或编码"这个兜底与用户
    看到的 "+0/-0" 同义反复 —— 用户依然不知道是行尾还是编码。

    所以把**远端原始字节**也传进来，两边各判一次再下结论。
    """
    full = os.path.join(ROOT, rel)
    try:
        with open(full, "rb") as f:
            lb = f.read()
    except OSError:
        lb = None
    rb = remote_bytes
    # M202（A169，低）为什么这里**不**再写 `if rb is None:` 分支：
    # 旧形态下预览会拉两次（行一次、字节一次），第二次失败时 `rb` 为
    # None 却仍落进下面那条兜底 —— 兜底措辞明确排除了行尾，而「远端字节
    # 没读到」恰恰意味着行尾**尚未排除**。实测（远端 `b"v1"` 缺换行、
    # 本地 `b"v1\n"`，只让第二次拉取失败）：差异正是行尾，文案却说
    #「行尾/编码**之外**」，与唯一真实成因相反。
    # N622 改成单次拉取之后，「第二次失败」这个场景不再存在 —— 取不到
    # 字节时 `old` 就是 None，预览走「远端内容拉不下来」那条诚实分支，
    # 根本到不了这里。所以本函数现在只会在**确实拿到了远端字节**时被
    # 调用；若将来新增调用点，那条分支才需要补回来（届时按 M202 的措辞）。
    sides = [x for x in (lb, rb) if x is not None]

    def _tag(b):
        t = []
        if b"\r\n" in b:
            t.append("CRLF/LF 行尾")
        if b and not b.endswith(b"\n"):
            t.append("末尾换行有无")
        try:
            b.decode("utf-8")
        except UnicodeDecodeError:
            t.append("非 UTF-8 字节")
        return t

    tags = []
    for b in sides:
        for t in _tag(b):
            if t not in tags:
                tags.append(t)
    if not tags:
        # 两边字节都读到了、却找不出差异 → 差异只可能在行尾归一化之外
        # 的地方（例如 BOM、NUL 字节）。如实说"未知"，不要谎报行尾。
        return _ZERO_DIFF_UNKNOWN
    return " / ".join(tags)


def _remote_blob_bytes(sha, size_hint=None):
    """远端 blob 的原始字节（N281 给 _zero_diff_reason 用）。

    与 _remote_blob_lines 同一条降级路径：拿不到就返回 None。
    """
    if size_hint is not None and size_hint > MAX_PREVIEW_BYTES:
        return None
    try:
        data = api("GET", f"/git/blobs/{sha}")
    except BaseException:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("encoding") != "base64" or not data.get("content"):
        return None
    try:
        return base64.b64decode(data["content"])
    except Exception:
        return None


def _local_lines(rel, mode):
    """本地文件文本行；symlink 直接展示它指向哪（这才是要推上去的内容）。"""
    full = os.path.join(ROOT, rel)
    if mode == "120000":
        # 不加 "-> " 前缀：远端 symlink blob 存的就是**目标路径字符串**本身，
        # 加了前缀后本地行与远端行永远差一个前缀，diff 恒为 +1/-1，
        # 于是「内容完全没变」也被显示成有改动 —— 预览层失真，
        # 而预览是推送前最后一道人工确认。
        # 而 `if mode_changed and add == 0 and sub == 0: continue` 救不了它，
        # 因为此时 add=1、sub=1，两边都不为 0。
        return [os.readlink(full)]
    try:
        if os.path.getsize(full) > MAX_PREVIEW_BYTES:
            return None
        with open(full, "rb") as f:
            if b"\0" in f.read(64 * 1024):      # 粗判二进制
                return None
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return None


def preview(todo, rstate, lmap):
    """预览：直接对比「本地文件 vs 远端最新内容」。

    不能用 `git diff HEAD`：本地 HEAD 只是拉取时的快照，而实际推送是以
    base_tree=head_sha（远端最新）为底的，远端比你新时 diff 基准就错了，
    会出现「确认的内容 ≠ 实际推上去的内容」。
    """
    # 每个文件都要一次 GET /git/blobs 拉远端内容：100 个文件 = 100 次
    # 额外请求，串行、无缓存，且 **--dry-run 也照发不误** —— 预演既不
    # 免费也不无害。叠加建 blob 本身，API 消耗直接翻倍，撞上限流后报
    # 403，而 403 提示只说「限流或权限」，用户联想不到是预览造成的。
    # 超过阈值只对前若干个做内容 diff，其余只报行数差（不需要拉远端）。
    PREVIEW_CAP = 20
    todo_all = list(todo)
    todo_preview = todo_all[:PREVIEW_CAP]
    skipped_preview = len(todo_all) - len(todo_preview)
    if skipped_preview > 0:
        print(f"\n  ! 共 {len(todo_all)} 个文件，只对前 {PREVIEW_CAP} 个做内容预览"
              f"（其余 {skipped_preview} 个省略，避免拉远端内容打爆 API 限流）")

    print(f"\n待推送 {len(todo_all)} 个文件（对比基准 = 远端 {BRANCH} 最新内容，非本地 HEAD）：")
    for rel in todo_preview:
        # N599（A155/A157，中）：`report_removals` 的三个打印点（renames /
        # entries / skipped）在 N22 那轮都补了 `_disp_path`，而本函数
        # —— **主流程上更早、且 todo 非空时必经的待推送清单** —— 一行
        # 都没补。
        #
        # 于是修复之后，含非 UTF-8 文件名的推送最先撞到的崩溃点仍然是
        # print，只是栈从 `report_removals` 挪到了 `preview`。N22 注释
        # 写的目标是「用户看到的是 UnicodeEncodeError 而不是『有个文件名
        # 有问题』」—— 崩溃点挪位置，这个目标就没达成。
        #
        # 而 `todo` 里的路径来自 `detect_changes()`，那一路没有编码过滤
        # （只有 gitignore 与缓存），`plan_removals` 里 added 侧的过滤
        # **不影响 todo**：两条独立的数据流，这里必须自己挡。
        # 显示一律走 `_disp_path`，IO 仍用真实 `rel`。
        _d = _disp_path(rel)
        try:
            size = os.path.getsize(os.path.join(ROOT, rel))
        except OSError:
            size = 0
        lmode = lmap.get(rel, ("100644", ""))[0]
        r = rstate.get(rel)
        if r is None:
            print(f"   A {_d}   新增（{size} B，mode {lmode}）")
            continue
        rmode, rsha = r
        mode_changed = (lmode != rmode)
        # N622（第二百〇五轮复现 / A169，低）：原来这里拉**行**、下面
        # 判零差异时又拉**字节** —— 同一个 sha、同一个端点，零差异文件
        # 必发两次 GET /git/blobs（实测 A 组 api 序列
        # `['/git/blobs/rsha1', '/git/blobs/rsha1']`）。
        # 预览对每个文件都要一次请求，翻倍直接撞限流，而 403 提示只说
        #「限流或权限」，用户联想不到是预览造成的。
        # 改成拉**字节一次**，行由字节派生。
        rb = _remote_blob_bytes(rsha, size_hint=size)
        if mode_changed:
            print(f"   M {_d}   mode {rmode} → {lmode}（权限/类型变更）")
        # N235：`size_hint` 是**零传入的死参数** —— 函数里那条
        # "超大就别拉"的短路由没有任何调用点能触发，于是本地文件
        # 明明超限（本地大小已知），仍会白拉一次远端 blob。
        #
        # 用本地大小做提示：本地超限则远端 blob 多半也超限，
        # 这一拉是纯浪费（大仓库预览时会明显变慢）。
        old, new = (_lines_from_blob_bytes(rb), _local_lines(rel, lmode))
        if old is None or new is None:
            # 成因要分清：old 为 None 也可能是**远端文件过大拉不下来**，
            # 而文案按本地大小写成「本地文件过大」会让排查方向完全错。
            if size > MAX_PREVIEW_BYTES:
                why = f"本地文件超过预览上限（{size} B）"
            elif old is None:
                why = "远端内容拉不下来或超过预览上限"
            else:
                why = "二进制或过大"
            if mode_changed:
                print(f"        （{why}，跳过内容预览）")
            else:
                print(f"   M {_d}   {why}，跳过内容预览（本地 {size} B）")
            continue
        if len(old) > MAX_PREVIEW_LINES or len(new) > MAX_PREVIEW_LINES:
            print(f"   M {_d}   远端 {len(old)} 行 → 本地 {len(new)} 行")
            continue
        # 必须 list() 物化：unified_diff 是**生成器**，
        # 第一个 sum() 会把它消费殆尽，第二个拿到空生成器 → 删除行恒为 0。
        # 实测推送前预览显示 "+1 / -0"，删了 30 行也显示 -0 ——
        # 这是推送前最后一道人工确认，展示失真等于确认无效。
        d = list(difflib.unified_diff(old, new, n=0))
        # A4-H2：不能靠 `not startswith("+++"/"---")` 排除文件头 —— 内容行
        # 本身以 `++` / `--` 开头时（C 的 `++i`、命令行 `--flag`、YAML/Markdown
        # 的 `---` 分隔线）会被当成文件头吃掉。实测新增一行 `++flag` 显示
        # `+0 / -0`、删除一行 `--opt` 同样显示 `-0`：用户看到「改了但零行
        # 差异」只能靠猜，而这是推送前最后一道人工确认。
        #
        # 文件头恒为**开头两行**（`--- ` / `+++ `），按位置截掉即可；
        # 此后任何以 `+` / `-` 开头的行都必然是内容行，不再需要前缀排除。
        _body = (d[2:] if len(d) >= 2 and d[0].startswith("---")
                 and d[1].startswith("+++") else d)
        add = sum(1 for l in _body if l.startswith("+"))
        sub = sum(1 for l in _body if l.startswith("-"))
        # N237：预览按"解码成文本再 splitlines()" 算，而 splitlines()
        # 会归一化行尾、忽略末尾空行。于是这几类**字节确实不同**
        # （sha 必然不同、文件确实在待推送列表里）的改动显示成 +0/-0：
        #   · 末尾换行（远端有 \n，本地没有）
        #   · CRLF → LF / LF → CRLF
        #   · 非 UTF-8 无效字节不同
        #
        # 用户看到「M f.txt  +0 / -0」会以为没事而放弃检查，
        # 实际推上去的是行尾被改过的版本 —— 这正是 autocrlf 分叉的
        # 典型来源。预览层失真比不预览更误导。
        #
        # 零改动但字节不同时，明说原因而不是显示 0。
        if add == 0 and sub == 0:
            # N281：纯 mode 变更（chmod +x，内容一字节未变）不该进归因
            # —— 报"仅行尾/编码差异"是**新增的误报**，根本没有那类差异。
            #
            # N620（第二百〇五轮 / A169，中）：判据原来是
            # `mode_changed and old == new`，而 old / new 是 splitlines()
            # 归一化之后的**行** —— 「内容一字节未变」与「只差末尾换行」
            # 在这两行里**不可区分**。实测三组（远端固定 `b"v1\n"`）：
            #   A 本地 `b"v1"`  + 100644 → 报「仅末尾换行有无」
            #   B 本地 `b"v1"`  + 100755 → 只报 mode，字节差异告知整条吞掉
            #   C 本地 `b"v1\n"` + 100755 → 与 B **逐字相同**
            # 即 B 的字节**真的不同**（缺末尾换行）却一个字都不说，而
            # 用户无法据此区分「纯 chmod」与「chmod + 行尾也变了」。
            # 改用**原始字节**判「纯 mode 变更」。
            lb = _local_blob_bytes(rel, lmode)
            if mode_changed and rb is not None and lb is not None and rb == lb:
                continue                   # 上面已报 mode 变更，内容一字节未变
            # N286：去掉 lmode —— _zero_diff_reason 从不看 mode。
            why = _zero_diff_reason(rel, remote_bytes=rb)
            # N621（同轮，中）：上面那支旧判据一旦为真就 `continue`，
            # 于是 `if mode_changed: print("（仅…差异）")` **永远走不到**
            # —— 600 次随机内容模糊里命中 0 次，结构上也不成立：
            # add + sub == 0 ⟹ old == new ⟹ mode_changed 时已在上面 continue。
            # 死分支会让人以为 mode 变更还有第二条告知路径。
            # 现在 mode 变更与非 mode 变更走同一句，分支自然消失（N620 的
            # C 组因此也能拿到字节差异告知）。
            if why == _ZERO_DIFF_UNKNOWN:
                # M202（A169，低）：「仅」+「字节存在差异」连读是语义重复；
                # 且这两个兜底本就是「说不准」，不该用「仅」把它说成确定。
                print(f"   M {_d}   +0 / -0（{why}，文本层无差异）")
            else:
                print(f"   M {_d}   +0 / -0（仅{why}，文本层无差异）")
            continue
        # N282 说的那个判据（`mode_changed and add == 0 and sub == 0`）
        # 已经不在了 —— 注释却还留着，指着一段不存在的代码。
        # 它与 N621 是同一件事的两半：判据自己成了死代码（N621），
        # 描述它的注释又留下来继续误导（A169 §四观察项）。一并清掉。
        print(f"   M {_d}   +{add} / -{sub}")

    # P2-2：被省略的文件必须**单独列出来**。
    # 它们仍会照常推送，却在上面一个字都看不到名字 —— 而「待推送 N 个文件」
    # 里是包含它们的。用户会以为自己看过全部 diff 了。
    if skipped_preview > 0:
        print(f"\n  · 以下 {skipped_preview} 个文件**未做内容预览**"
              f"（超出 {PREVIEW_CAP} 个上限），仍会照常推送：")
        for rel in todo_all[PREVIEW_CAP:]:
            print(f"     {_disp_path(rel)}")


def default_msg(todo):
    if len(todo) == 1:
        return f"chore: 更新 {todo[0]}"
    if len(todo) <= 3:
        return "chore: 更新 " + "、".join(todo)
    return MSG_FALLBACK


# ---------------------------------------------------------------- 参数

#: **带值**的长选项（`--x=v` 与 `--x v` 两种写法都要认）。
#:
#: N134：原先 --x=v 的拆分只认一个硬编码白名单（9 个），
#: --audit=5 之类就报「未知参数」。而白名单的致命处是
#: **新增带值参数时又会漏** —— 与 N102 同源。
#:
#: 集中声明一处，parse_args 与 --help 都从它取，不再各写一份。
VALUE_OPTS = (
    "--root", "--repo", "--state", "--stale-days", "--branch",
    "--method", "--force-file", "--delete-branch", "--message",
    "--audit",
    # N131：--allow-overwrite 是 --force-file 的**别名**（见 USAGE），
    # 同样是带值参数，却只有下面那处 `startswith("--allow-overwrite=")`
    # 专用分支认它 —— 于是它成了唯一不走 VALUE_OPTS 的带值选项。
    # 后果是两种写法走两条路：派生出一份"别名专用"的实现，
    # 而 VALUE_OPTS 的注释明写"新增带值参数时又会漏"。
    # 统一进白名单后，别名与正名同走通用拆分，专用分支即可删掉。
    "--allow-overwrite",
)


def parse_args(argv):
    opts = {"dry": False, "yes": False, "init": False, "reset": False,
            "no_delete": False,
            "enable_delete": False,
            "force": False, "ignore_pending": False,
            "mark_synced": False, "msg": None,
            "pull": False, "resolve": False, "force_files": [],
            "direct": False, "branch": None, "merge": False, "status": False,
            "scope": False, "mine": False,
            "close_pr": False, "prune": False, "new_task": False, "hold": False,
            "delete_branch": None, "keep_on_conflict": False,
            "quiet": False,
            "allow_stale": False, "clean_conflicts": False,
            # N11：删除的放行开关**独立于** --force-overwrite。
            # force 在推送路径的语义是「覆盖远端」，用它同时放行删除
            # 等于让用户以为自己在"覆盖"，实际授权了"删除他人改动过的文件"
            # —— 删除比覆盖更不可逆（覆盖的内容至少还在旧 commit 里）。
            # 作者已在冲突保护那处引入独立的 ignore_pending（同一族问题），
            # 删除这一处此前没有对应开关。
            "force_delete": False,
            "api": False,
            "audit": False, "audit_limit": 20,
            "method": "squash", "stale_days": MERGED_GRACE_DAYS,
            "state_path": None, "help": False,
            # 阶段 0：仓库位置不再硬编码。见 resolve_config 的说明。
            "root": None, "repo": None, "json": False}
    rest, i = [], 0
    while i < len(argv):
        a = argv[i]
        # N82：带值参数此前只认 `--root /tmp/x`（空格写法），
        # `--root=/tmp/x` 直接报「未知参数」。而 --branch / --method 等
        # 是支持 = 写法的 —— 同一个解析器里两套写法规则，用户无从判断。
        # 这 4 个又恰好是 USAGE 里一个都没提到的（见 N77），
        # 于是"既不知道有、知道了还可能写错"。
        #
        # 统一：进循环就把 `--x=v` 拆成 `--x` `v` 两个数，
        # 后面的分支不必各自处理 = 写法（否则新增参数又会漏）。
        if a.startswith("--") and "=" in a and not a.startswith("--message"):
            _k, _v = a.split("=", 1)
            # N134：白名单只列了 9 个，而 --audit=5 等仍不支持。
            # 白名单的问题是**新增带值参数时又会漏** ——
            # 与 N102（--force-delete 写进 help 却解析不了）同源。
            # 改成通用规则：只要这个长选项**确实带值**，就拆。
            # N239：VALUE_OPTS 已含 --message，后半句永不改变结果。
            # 留着它会让人以为 VALUE_OPTS 不完整，进而怀疑其他带值参数
            # 是否也靠这种特例兜着 —— 值得怀疑的地方不写注释就会反复被查。
            if _k in VALUE_OPTS:
                argv = argv[:i] + [_k, _v] + argv[i + 1:]
                a = _k
        if a in ("--help", "-h"):
            # 必须有：没有 -h 时打错参数会落到「未知参数」分支，
            # 用户拿不到任何用法说明，只能去翻源码。
            print(USAGE)
            raise SystemExit(0)
        elif a in ("--state",):
            # 报错文案一直建议「请换用对应的 STATE_PATH」，但此前根本没有
            # 这个参数 —— 建议的操作不存在。同一台机器推多个仓库时，
            # 每个仓库需要各自的基线文件。
            if i + 1 >= len(argv):
                raise SystemExit("--state 需要一个路径参数")
            opts["state_path"] = argv[i + 1]
            i += 1
        elif a == "--dry-run":
            opts["dry"] = True
        elif a in ("--yes", "-y"):
            opts["yes"] = True
        elif a == "--init-baseline":
            opts["init"] = True
        elif a == "--reset-baseline":
            opts["reset"] = True
        elif a == "--mark-synced":
            opts["mark_synced"] = True
        elif a == "--pull":
            opts["pull"] = True
        elif a == "--api":
            # 强制走 API 通道：git 权限受限、或想让 blob 逐条留痕时用。
            opts["api"] = True
        elif a == "--no-delete":
            opts["no_delete"] = True
        elif a == "--delete":
            opts["enable_delete"] = True
        elif a == "--resolve":
            opts["resolve"] = True
        elif a == "--direct":
            opts["direct"] = True
        elif a == "--status":
            opts["status"] = True
        elif a == "--scope":
            opts["scope"] = True
        elif a == "--mine":
            # 只输出 MINE 的文件路径（一行一个，可管道）。
            # 单独可解析，但与 --scope 同源：不另起一套判据。
            opts["mine"] = True
        elif a == "--merge":
            opts["merge"] = True
        elif a == "--close-pr":
            opts["close_pr"] = True
        elif a == "--prune":
            opts["prune"] = True
        elif a == "--audit":
            opts["audit"] = True
            # 可选：--audit N 看最近 N 条
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                i += 1
                opts["audit_limit"] = int(argv[i])
        elif a == "--force-delete":
            # N102：USAGE 里写了这个参数（第 39 行），parse_args 却不认 ——
            # 用户照 --help 输入即报「未知参数」。写进文档的选项必须能解析。
            # 它只放行删除侧的判据，**不等同** --force-overwrite
            #（后者绕过的是文件级基线校验，前者只绕过删除判据）。
            opts["force_delete"] = True
        elif a == "--clean-conflicts":
            # 清理 .push-conflicts 里的残留副产物。
            # 这个目录**只增不减**：删除只发生在 --resolve。用户手工解决
            # 冲突（改完文件直接推）、或放弃那个改动后，副产物就永久留下
            # —— 在仓库外、--prune 不管、原先也没有任何命令能清。
            # 而里面是**完整的远端明文**，属于被遗忘的敏感数据副本。
            opts["clean_conflicts"] = True
        elif a == "--new-task":
            opts["new_task"] = True
        elif a == "--hold":
            opts["hold"] = True
        elif a == "--keep-on-conflict":
            opts["keep_on_conflict"] = True
        elif a == "--root":
            # 覆盖硬编码的仓库路径。不传给 git -C 的默认值来自 cwd。
            if i + 1 >= len(argv):
                raise SystemExit("--root 需要一个路径参数")
            opts["root"] = argv[i + 1]
            i += 1
        elif a == "--repo":
            # 覆盖硬编码的 owner/repo。不写则从 git remote origin 推断。
            if i + 1 >= len(argv):
                raise SystemExit("--repo 需要 owner/repo 或 repo 参数")
            opts["repo"] = argv[i + 1]
            i += 1
        elif a == "--json":
            # 探路：只在只读子命令（--status / --prune）上生效。
            # 推送路径涉及大量人工确认与交互文案，暂时不覆盖 ——
            # 半结构化的输出比没有更危险（人会拿它当可靠依据）。
            opts["json"] = True
        elif a in ("--quiet", "-q"):
            # N295（第一百〇二轮）：`--quiet` 早就有完整的语义实现
            # （Ui.quiet：压掉 info 级噪音、warn/error/ok 保留），
            # 却**没有任何 CLI 入口** —— parse_args 不认它，
            # 于是 `Ui(quiet=True)` 在生产侧永远构造不出来。
            #
            # 它一直"潜伏"着没出事，是因为压根用不了。而 A32 把
            # warn/error 接上之后，这条的**后果方向变了**：
            #   · 修 A32 之前：49 条全是 info → quiet 会把**全部**输出压掉
            #   · 修 A32 之后：防护指引是 warn/error → quiet 下保留，
            #     压掉的确实只是进度噪音，与 docstring 承诺一致
            # 也就是说 A32 是 --quiet 能安全接线的前提，
            # 两者必须一起才有意义。
            opts["quiet"] = True
        elif a == "--allow-stale-merge":
            # 放行为「主干在我建分支后前进过、且动了我推的文件」。
            # 只在合并阶段生效；不绕过推送前的任何一层校验。
            opts["allow_stale"] = True
        elif a == "--delete-branch":
            if i + 1 >= len(argv):
                raise SystemExit("--delete-branch 缺少参数")
            opts["delete_branch"] = argv[i + 1]
            i += 1
        elif a.startswith("--delete-branch="):
            opts["delete_branch"] = a.split("=", 1)[1]
        elif a in ("--branch", "-b"):
            if i + 1 >= len(argv):
                raise SystemExit("--branch 缺少参数")
            opts["branch"] = argv[i + 1]
            i += 1
        elif a.startswith("--branch="):
            opts["branch"] = a.split("=", 1)[1]
        elif a == "--method":
            if i + 1 >= len(argv):
                raise SystemExit("--method 缺少参数")
            opts["method"] = argv[i + 1]
            i += 1
        elif a.startswith("--method="):
            opts["method"] = a.split("=", 1)[1]
        elif a == "--stale-days":
            if i + 1 >= len(argv):
                raise SystemExit("--stale-days 缺少参数")
            raw = argv[i + 1]
            try:
                opts["stale_days"] = int(raw)
            except ValueError:
                raise SystemExit(
                    f"--stale-days 需要一个整数天数，收到 {raw!r}\n"
                    f"  用法：--stale-days 7")
            if opts["stale_days"] < 0:
                raise SystemExit(f"--stale-days 不能为负数：{raw}")
            i += 1
        elif a == "--force-overwrite":
            opts["force"] = True
        elif a == "--ignore-pending":
            # 独立于 --force-overwrite：--pull 场景里「跳过上次未解决的
            # 冲突」与「覆盖远端」是两件事，不能共用一个开关。
            opts["ignore_pending"] = True
        elif a in ("--force-file", "--allow-overwrite"):
            # 细粒度放行：只覆盖显式点名的文件，别把所有 blocked 一起放过去。
            if i + 1 >= len(argv):
                raise SystemExit("--force-file 缺少参数")
            opts["force_files"].append(argv[i + 1])
            i += 1
        # N131：这里原先同时兜 `--force-file=` 与 `--allow-overwrite=`
        # 两种等号写法。而 `--force-file` 已在 VALUE_OPTS 里（N82 起），
        # 通用拆分会先把它拆成两个 token —— 于是这一半**永不命中**，
        # 是死分支（实测：改成 `elif False` 后 `--force-file=a.txt`
        # 照样解析成 ['a.txt']）。
        #
        # 留着它的代价不是多跑一次判断，而是误导：注释/分支描述了一种
        # 不可能发生的情形，下一位维护者会照它去理解或"修复"参数解析。
        #
        # 另一半（--allow-overwrite）当时**不是**死的 —— 它不在
        # VALUE_OPTS 里，全靠这条分支。所以不能整条删：
        # 先把它加进 VALUE_OPTS（见上），再删整条，两种写法才同源。
        elif a in ("-m", "--message"):
            if i + 1 >= len(argv):
                raise SystemExit("-m/--message 缺少参数")
            opts["msg"] = argv[i + 1]
            i += 1
        elif a.startswith("--message="):
            opts["msg"] = a.split("=", 1)[1]
        elif a == "--":
            # 之后一律当路径：以 `-` 开头的文件名（虽然罕见）否则会被
            # 当成未知选项，永远推不上去，且报错是「未知参数」而非
            # 「文件不存在」—— 排查方向完全跑偏。
            rest.extend(argv[i + 1:])
            break
        elif a.startswith("--"):
            raise SystemExit(f"未知参数: {a}")
        else:
            rest.append(a)
        i += 1

    # 放在循环后统一校验：--method 和 --method=x 两种写法都能覆盖到，
    # 不必在两处各写一遍。本地校验比把非法值发给 GitHub 再收一串 422 JSON
    # 好排查得多。
    if opts["method"] not in VALID_MERGE_METHODS:
        raise SystemExit(
            f"--method 只支持 {' / '.join(VALID_MERGE_METHODS)}，"
            f"收到 {opts['method']!r}")
    return opts, rest


# ---------------------------------------------------------------- 主流程

def _git_toplevel(cwd=None):
    """当前目录所在的 git 仓库根；不是仓库返回 None。

    用 -C 而不是切进程工作目录：改 cwd 会影响并发跑着的其他测试。
    """
    try:
        r = subprocess.run(["git", "-C", cwd or os.getcwd(),
                            "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        top = r.stdout.strip()
        return top or None
    except (OSError, subprocess.SubprocessError):
        return None


# N81：主机白名单。本脚本只能操作 api.github.com，
# 从别的主机推断出的 slug 会被映射成 GitHub 上的同名仓库。
# 用环境变量可放开（GitHub Enterprise 的自建域名等），
# 但必须显式声明，而不是默认接受一切。
def _github_hosts():
    """主机白名单（**运行时**求值）。

    N113：原先是模块级常量，import 时算一次。而同文件里其它配置
    （PUSH_API_ROOT / PUSH_API_REPO / PUSH_API_ENABLE_DELETE）都是
    函数内动态读取 —— 同一套配置机制出现两种时序。

    后果：把它当库调用、或同进程内连续推两个仓库（Runtime 的注释
    就拿这个场景举例）时，第二次设的环境变量**不生效**。
    """
    return tuple(
        h.strip().lower()
        for h in os.environ.get("PUSH_API_GITHUB_HOSTS", "github.com").split(",")
        if h.strip())


def detect_repo_slug(root):
    """从 git remote origin 解析 (owner, repo)。解析不出返回 (None, None)。

    支持两种写法：
      https://github.com/owner/repo.git
      git@github.com:owner/repo.git
    也容忍没带 .git 后缀、以及 SSH 别名（git@github.com-foo:o/r.git）。

    为什么必须能推断：硬编码 OWNER/REPO 意味着「脚本与仓库一一对应」，
    而它一旦被复制到别处（或本仓库改名），就会静默操作**另一个**远端仓库。
    这类错误的后果是直接把内容推到错误的目的地，且全程零提示。
    """
    if not root:
        return None, None
    try:
        r = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None, None
        url = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, None
    if not url:
        return None, None
    u = url.strip()
    if "://" in u:
        # URL 形式：ssh://git@github.com/o/r.git、https://token@github.com/o/r.git
        rest = u.split("://", 1)[1]
        # N81 补充：file:// 是无主机的本地协议。它进的是这一支，
        # 而 head 解析出来为空 → 下方主机校验被"空主机"跳过 → 放行。
        # 实测 file:///tmp/x/repo.git 会被解析成 ('x','repo')，
        # 随即映射到 GitHub 上的同名仓库。必须显式挡掉。
        if u.split("://", 1)[0].lower() != "" and not u.lower().startswith(
                ("http://", "https://", "ssh://", "git://")):
            print(f"  ! origin 是 {u.split('://', 1)[0]}:// 协议（无远端主机），"
                  f"本脚本只走 api.github.com，不会据此推断仓库。"
                  f"请用 --repo owner/name 显式指定。")
            return None, None
        # 主机位可能带 userinfo（git@ / token@）
        head, sep, tail = rest.partition("/")
        if "@" in head:
            head = head.split("@", 1)[1]
        if not sep:
            return None, None
        _host = head
        path = tail
    elif ":" in u:
        # scp 形式：git@github.com:owner/repo.git
        #
        # 必须先于按 `/` 切：`github.com:owner/repo.git` 里也有 `/`，
        # 若先按 / 切会把 `repo.git` 当成整条路径，owner 丢失 → 解析失败。
        # 实测踩到：这是最常见的 SSH 远程写法，解析错等于功能不可用。
        rest = u.split("@", 1)[1] if "@" in u else u
        _host = rest.split(":", 1)[0] if ":" in rest else ""
        path = rest.split(":", 1)[1] if ":" in rest else None
        if not path:
            return None, None
    else:
        return None, None
    # N81：此处此前**从不校验主机**，函数体内 host 出现 0 次。
    # 于是 GitLab / GitHub Enterprise / Bitbucket / file:// 的 remote
    # 全被解析成合法 slug，随后 BASE 被设成 api.github.com/repos/{o}/{r}
    # —— 把一个非 GitHub 仓库，静默映射成 GitHub 上的**同名仓库**。
    #
    # 而 --status 只展示 `o/r（来源：git）`、不展示主机，
    # 对 GitLab 仓库这行输出与真 GitHub 仓库完全一样，用户看不出异常。
    #
    # 后果：同名仓库在 GitHub 上若存在，内容就被推到那里 ——
    # 正是注释点名要防的"推到错误目的地，且全程零提示"。
    # 本脚本只能操作 api.github.com，非 GitHub 的 remote 下拒绝并
    # 要求显式 --repo，而不是猜。
    _h = (_host or "").split("@")[-1].split(":")[0].lower().strip()
    # SSH 别名（git@github.com-work:team/proj.git）要识别：
    # 它是 ~/.ssh/config 里 Host 段的别名，真实主机仍是 github.com。
    # 按前缀匹配：别名以白名单主机开头，后接 "-" 或 "." 分隔的后缀。
    # 直接判相等会把这个常见写法挡掉（实测踩到）。
    _hosts = _github_hosts()
    _ok = _h in _hosts or any(
        _h.startswith(gh + "-") or _h.startswith(gh + ".")
        for gh in _hosts)
    if _h and not _ok:
        print(f"  ! origin 的主机是 {_h or '(空)'}，不是 github.com："
              f"本脚本只走 api.github.com，不会据此推断仓库。"
              f"请用 --repo owner/name 显式指定。")
        return None, None
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None, None
    return parts[-2], parts[-1]


# ---- 传输通道探测：git 协议到底通不通 ----
#
# 硬约束（不是设计选择）：本环境 github.com 被网关拦成 403，只有
# api.github.com 通。远端因此只能走 Git Data API —— 本脚本 5500 行里的
# 绝大部分复杂度（自造基线系统 + 四层防护）都是这一个约束的直接后果。
#
# 必须**显式探测**而不是默默假设，两个理由：
#   · 换环境后如果 git 其实通了，用户该立刻知道：那套基线系统与四层
#     防护可以整块删掉，这才是真正的解脱，而不是继续养着 5500 行。
#   · 反过来，新人遇到 403 会先怀疑 token 配错，白白排查半天 —— 而
#     403 恰恰说明 token 根本没参与（网关在更外层拦的）。
TRANSPORT_PROBE_TIMEOUT = 5


def probe_git_transport(force=False):
    """探测 github.com 的 git 通道是否可达。返回 dict；**永不抛异常**。

    判据是 curl github.com 根路径的 HTTP 码：
      · 200       → 通（可以直接 git push）
      · 403       → 被网关拦（本环境常态，走 API 是对的）
      · 空 / 其它 → 未知，按 API 路径继续，**不阻断**

    用 curl 而非 git ls-remote：拦截发生在 HTTP 层，curl 一个请求就能
    区分，快一个数量级；ls-remote 要建连接再协商，慢且噪声大。

    探测失败一律按「未知」处理并继续走 API —— 探测只是**告知**用户，
    它不能成为阻断推送的理由（API 路径本身是可用且经过验证的）。
    """
    if _RT.transport_probe is not None and not force:
        return _RT.transport_probe
    if os.environ.get("PUSH_API_NO_PROBE"):
        _RT.transport_probe = {"state": "skipped", "code": "",
                            "detail": "PUSH_API_NO_PROBE 已设置"}
        return _RT.transport_probe
    try:
        # N199：两步必须探**同一个目标**。
        # curl 原先探 github.com 根，而 ls-remote 探的是实际推送地址
        # —— 实测出现过"curl 通 github.com + ls-remote 探别的仓库"，
        # 于是 state=="ok" 只证明了"能访问 github.com"，
        # 与"能推到本次的目标仓库"根本不是一回事。
        #
        # 改成从实际推送 URL 推导主机根，两步同源。
        _probe_url = _git_push_url() or "https://github.com/"
        _m = re.match(r"(https?://[^/]+)", _probe_url)
        _host_root = (_m.group(1) if _m else "https://github.com") + "/"
        pr = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(TRANSPORT_PROBE_TIMEOUT),
             _host_root],
            capture_output=True, text=True,
            timeout=TRANSPORT_PROBE_TIMEOUT + 3)
        code = (pr.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        code = ""
    if code != "200":
        st = "blocked" if code == "403" else "unknown"
        _RT.transport_probe = {"state": st, "code": code}
        return _RT.transport_probe
    # HTTP 200 只证明**网页**可达，不能证明能 push：
    # 认证可能失败、git 协议（非 HTTP）可能被单独拦、仓库权限可能不足。
    # 本环境实测就是 200 通、git ls-remote 403 不通 ——
    # 只按 curl 判定会把"能 git push"说成真的，用户照做才撞墙。
    #
    # 所以 200 之后必须再用**真实 push 路径**确认一次：
    # 用 ls-remote 而非真 push，因为它只读、无副作用，
    # 但走的正是 push 要用的同一条协议与认证。
    okpush, detail = _git_can_push()
    _RT.transport_probe = {
        "state": "ok" if okpush else "blocked",
        "code": code,
        "git": okpush,
        "git_detail": detail,
    }
    return _RT.transport_probe


def _git_can_push():
    """用 ls-remote 确认 git 推送通道真的可用（只读、无副作用）。

    ls-remote 走的是与 push 相同的协议、相同的认证、相同的主机与端口，
    但它不写任何东西 —— 用来做探测正合适。

    返回 (是否可用, 说明)。**永不抛异常**。
    """
    if not _git_available():
        return False, "git 不可用或未安装"
    url = _git_push_url()
    if not url:
        return False, "拿不到远端地址（origin 未配置且 --repo 无法映射）"
    env = _git_auth_env()
    try:
        pr = subprocess.run(
            ["git", "ls-remote", "--exit-code", url, "HEAD"],
            capture_output=True, text=True,
            timeout=TRANSPORT_PROBE_TIMEOUT + 5,
            cwd=ROOT if os.path.isdir(ROOT) else None,
            env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"ls-remote 失败：{type(e).__name__}"
    if pr.returncode == 0:
        return True, "ls-remote 成功"
    err = (pr.stderr or "").strip().splitlines()
    return False, (err[-1][:120] if err else f"退出码 {pr.returncode}")


def _git_auth_env():
    """给 git 子进程准备带认证的环境。

    token 走**环境变量**而不是 `git -c http.extraHeader=...`：
    后者把 token 放在命令行参数里，同机任何用户 ps 一下就看到
    （脚本自己的注释反复强调过 token 不能进命令行）。
    GIT_CONFIG_* 是 git 官方支持的配置注入方式，不出现在 argv 里。
    """
    env = os.environ.copy()
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if tok:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Bearer {tok}"
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git_available():
    try:
        pr = subprocess.run(["git", "--version"], capture_output=True,
                            text=True, timeout=10)
        return pr.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _git_channel_ok():
    """本次是否用 git 通道送出。

    判据只有一条：探测确认 git push 通道真的可用。
    探测结果有缓存（进程内一次），反复调用无成本。

    用户可选择强制走 API（比如想让 blob 逐条留痕、或 git 权限受限
    但 API 可用），关掉用 --api。
    """
    if _RT.no_git:
        return False
    return probe_git_transport().get("state") == "ok"


def git_send(target_ref, base_sha, todo, rm_paths, msg, lmap=None):
    """用 git 原生能力送出本次改动（替代 API 的建对象 + 更新 ref）。

    只在探测确认 git 通道可用时调用。**防护全部在调用方跑完** ——
    git 在这里只当传输管道，不参与任何判定（用户选择的形态）。

    为什么不用 `git add` + `git commit`：
      那会动用户的工作区与真实索引，可能把用户没打算提交的东西
      一起卷进来（脚本自己的注释反复强调过不能碰用户索引）。
    改用 GIT_INDEX_FILE 建**临时索引**，base 用远端 main ——
      与 API 路径的 base_tree / parent 完全等价，且不碰用户任何状态。

    返回 commit sha。失败抛 SystemExit —— 由调用方决定是回退 API
    还是直接报错（回退更安全：探测说通、实际 push 撞认证的情况很常见）。
    """
    url = _git_push_url()
    if not url:
        raise SystemExit("拿不到远端地址，无法用 git 送出")
    work = tempfile.mkdtemp(prefix="pushsync-git-")
    idx = os.path.join(work, "index")
    env = _git_auth_env()
    env["GIT_INDEX_FILE"] = idx
    # 提交必须有身份，否则没有 user.email 的机器上 commit-tree 直接失败，
    # 而报错信息（"Please tell me who you are"）完全指不到真实原因。
    env.setdefault("GIT_AUTHOR_NAME", "push-sync")
    env.setdefault("GIT_AUTHOR_EMAIL", "push-sync@local")
    env.setdefault("GIT_COMMITTER_NAME", "push-sync")
    env.setdefault("GIT_COMMITTER_EMAIL", "push-sync@local")
    env["GIT_TERMINAL_PROMPT"] = "0"

    def _grun(args, inp=None):
        # N190：原先没开 text=True，于是 pr.stdout 是 **bytes**、
        # input 也必须是 bytes。而 cacheinfo 是用 f-string 拼的 str ——
        # 只要有文件要推就 TypeError（必崩）。
        # 且不开 text 时 refspec 会变成 "b'xxx'" 这种字面量，纯删除也崩。
        #
        # N192：不捕 TimeoutExpired。而 push 大仓库很容易超时 ——
        # 异常冒出 git_send 后，调用方的 `except SystemExit` 接不住它，
        # 于是**回退 API 的兜底整个失效**（改动丢在半路）。
        # 这里统一转成 SystemExit，让调用方能按既有路径回退。
        # N206：text=True 是**错的** —— hash-object --stdin 收的是
        # 原始字节：二进制会被破坏、CRLF→LF 会让 sha 对不上。
        # 推上去是错内容，而 sha 不同还不一定能被发现。
        # 正确做法：保持二进制管道，只在需要字符串处手动解码（_b2s）。
        if inp is not None and not isinstance(inp, bytes):
            inp = inp.encode("utf-8")
        try:
            pr = subprocess.run(["git"] + args, capture_output=True,
                                input=inp, env=env, cwd=ROOT,
                                timeout=GIT_TRANSPORT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise SystemExit(
                # M57（P1）：args[:2] 里可能是 origin 的原始 URL，而它
                # 内嵌 token（CI 的 x-access-token 是标准做法）。
                f"git {' '.join(_scrub_url(a) for a in args[:2])} 超时"
                f"（>{GIT_TRANSPORT_TIMEOUT}s）。"
                f"改动都还在本地，可改用 --api 走 API 通道重试。")
        except (OSError, subprocess.SubprocessError) as _e:
            raise SystemExit(
                f"git {' '.join(_scrub_url(a) for a in args[:2])}"
                f" 无法执行：{type(_e).__name__}")
        if pr.returncode != 0:
            err = _b2s(pr.stderr or pr.stdout).strip()
            raise SystemExit(
                f"git {' '.join(_scrub_url(a) for a in args[:2])} 失败"
                f"（退出码 {pr.returncode}）：{_scrub_url(err[:200])}")
        return _b2s(pr.stdout).strip()

    try:
        # 1) 先拿到 base 对象：本地可能根本没有它（从未 fetch 过）。
        #    read-tree 需要对象真实存在，否则 invalid object。
        _grun(["fetch", "--no-tags", "--no-write-fetch-head", url, base_sha])
        # 2) 临时索引 = 远端 main 的树
        _grun(["read-tree", base_sha])
        # 3) 删除条目（与 API 路径的 sha:null 等价）
        if rm_paths:
            # M4（A11 / A24 真实 git 实证）：路径以 `-` 开头时 git 会把它
            # 当成选项 —— 实测 `--dry-run.txt` → exit=129
            # "error: unknown option 'dry-run.txt'"，整批失败。
            # 加 `--` 分隔后 exit=0。
            #
            # 本脚本的路径来自 git status，以 `-` 开头完全合法
            #（文件名允许前导连字符），不是理论风险。
            _grun(["update-index", "--force-remove", "--"]
                  + list(rm_paths))
        # 4) 本次要推的文件：内容写进对象库，再挂到临时索引
        cacheinfo = []
        for rel in todo:
            full = os.path.join(ROOT, rel)
            mode = (lmap or {}).get(rel, ("100644", ""))[0]
            if mode == "120000":                      # symlink：内容是目标路径
                raw = os.readlink(full).encode("utf-8", "surrogateescape")
            else:
                with open(full, "rb") as f:
                    raw = f.read()
            # -w 真正写入对象库，否则 write-tree 找不到 blob
            bsha = _grun(["hash-object", "-w", "--stdin"], inp=raw)
            # N191：`--index-info` 从 stdin 读的是 **mode SP sha TAB path**
            #（即 `git ls-files -s` 的输出格式），不是 `--cacheinfo`
            # 的逗号格式。用逗号会 fatal: malformed index info。
            cacheinfo.append(f"{mode} {bsha}\t{rel}")
        if cacheinfo:
            _grun(["update-index", "--index-info"],
                  inp="\n".join(cacheinfo) + "\n")
        # 5) 建树 + 提交（parent 与 API 路径一致 = base_sha）
        tree = _grun(["write-tree"])
        # N378：同上。git 能展开短 sha，所以这里**不会**报错 ——
        # 也就正是它会在回退到 API 时才炸的原因。提前拦下，两通道一致。
        if base_sha and not _is_sha(base_sha):
            raise SystemExit(
                f"取到的远端 head 不是合法 sha：{base_sha!r}"
                f"（git 能展开短 sha，但回退到 API 时会 422）")
        commit = _grun(["commit-tree", tree, "-p", base_sha, "-m", msg])
        # 6) 推送。不用 --force：非快进由远端拒绝，与 API 路径的
        #    force=False 同一语义 —— 这是第二层防护在 git 侧的对应。
        _grun(["push", url, f"{commit}:refs/heads/{target_ref}"])
        return commit
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _git_push_url():
    """远端推送地址。

    N198：**绝不能无条件采用 origin**。
    用户用 `--repo A` 指定要推到 A，而本地 origin 可能指向完全无关的
    B（沙盒里另一批改动、fork、老仓库…）。实测 git 通道会把改动
    **推到 origin 的 B**，而脚本其余部分（API 通道）推的是 A ——
    同一个命令两处行为不一致，且推错仓库最难发现（远端看得到，本地没有）。

    正确顺序：
      1. OWNER/REPO 确定时，以它为准拼 HTTPS 地址（与 API 通道同目标）
      2. 只有当 origin 解析出的 owner/repo **与它一致**时，才改用
         origin 的原始 URL —— 这样能保留 SSH / 自定义协议等用户设置
      3. 仓库仍是编译期默认值时不拼任何地址
    """
    if not (OWNER and REPO):
        return ""
    if (OWNER, REPO) == (_DEFAULT_OWNER, _DEFAULT_REPO):
        # 推断没成功，推过去会写到另一个仓库 —— 绝不生成地址。
        return ""
    target = f"{OWNER}/{REPO}"
    try:
        pr = subprocess.run(["git", "remote", "get-url", "origin"],
                            capture_output=True, text=True, timeout=15,
                            cwd=ROOT if os.path.isdir(ROOT) else None)
        url = (pr.stdout or "").strip() if pr.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        url = ""
    if url and _url_matches(url, target):
        return url
    return f"https://github.com/{target}.git"


def _url_matches(url, target):
    """把 git 远端地址解析成 owner/repo，与目标比对。

    同时覆盖三种常见写法：
      https://github.com/o/r.git
      git@github.com:o/r.git
      ssh://git@github.com/o/r
    非 github.com 的主机**一律不算匹配** —— GitLab / 自建实例与
    GitHub API 通道不是同一个远端（N81 已就"只认 github"做过硬拦）。
    """
    # N212：`"github.com" in u` 是子串判据 —— 冒充域名全部通过：
    #   https://github.com.evil.com/o/r.git   → 匹配（真凶）
    #   https://mygithub.com/o/r.git          → 匹配
    #   https://evil.net/?x=github.com/o/r    → 匹配
    # 而 git 通道会**照着这个 URL 推送**，等于把改动送到攻击者主机。
    #
    # 必须解析出**真正的主机**再比对，且只认 github.com 与 www.github.com。
    u = (url or "").strip()
    host, tail = _split_git_url(u)
    if host not in ("github.com", "www.github.com"):
        return False
    parts = [x for x in tail.split("/") if x]
    if len(parts) < 2:
        return False
    own, rep = parts[-2], parts[-1]
    if rep.endswith(".git"):
        rep = rep[:-4]
    return f"{own}/{rep}" == target


def _split_git_url(u):
    """把 git 远端地址拆成 (主机, 路径)。

    覆盖四种常见写法：
      https://github.com/o/r.git
      ssh://git@github.com/o/r
      git@github.com:o/r.git
      github.com/o/r

    传入 None / 非字符串时返回 ("", "") 而不是抛 TypeError：
    解析失败要能被上层当作「这个远端不可用」处理，
    而抛异常会让整次命令崩掉（文档：异常输入不抛）。
    """
    if not isinstance(u, str) or not u:
        return ("", "")
    s = u
    if "://" in s:
        rest = s.split("://", 1)[1]
        if "@" in rest:                      # ssh://git@host/path
            rest = rest.split("@", 1)[1]
        if "/" in rest:
            host, tail = rest.split("/", 1)
        else:
            host, tail = rest, ""
        # 去掉 host 里的端口与用户残留
        host = host.split("@")[-1].split(":")[0]
        return host.lower(), tail
    if ":" in s and "@" in s.split(":", 1)[0]:
        # git@github.com:o/r.git —— 注意与 https 的端口写法区分
        left, right = s.split(":", 1)
        return left.split("@")[-1].lower(), right
    if "/" in s:
        host, tail = s.split("/", 1)
        return host.split(":")[0].lower(), tail
    return s.lower(), ""


def _transport_note():
    """把探测结果转成一行**能照着做**的话。"""
    t = probe_git_transport() or {}
    st = t.get("state")
    if st == "ok":
        return ("github.com 可达（HTTP 200）→ **可以直接用 git push**。\n"
                "     本脚本的自造基线系统与四层防护是为「只有 API 通」的环境写的，\n"
                "     git 可用时它们纯属负担，建议切回原生 git。")
    if st == "blocked":
        return ("github.com 被拦（HTTP 403）→ 只能走 api.github.com，本环境常态。\n"
                "     ⚠️  403 不等于 token 无效：拦截在更外层，token 根本没参与。")
    if st == "skipped":
        return f"未探测（{t.get('detail', '')}）"
    return ("github.com 探测无结果（超时或网络异常），按 API 路径继续。\n"
            "     这不影响推送 —— 探测只是告知，不参与任何拦截判定。")


#: N324：本次运行是否退回了「仓库外的全局基线」（legacy 分支）。
#: 非空即 (旧路径, 新路径)。推送路径据此补一次提示 ——
#: state_hint 只在 --status 打印，正常推送一声不响。
_LEGACY_STATE_ACTIVE = None


def _legacy_state_path():
    """旧版状态文件路径。仅为兼容保留，见 resolve_config 的说明。

    N316（第九十五轮，中危）：原先无条件返回 `_DEFAULT_STATE_PATH`
    （`/data/workspace/.push-sync.json`，**编译期常量**）—— 它不含 ROOT、
    不含仓库名，与本次要推的仓库毫无关系。

    实测（`--root` 指向另一个仓库 B）：

        STATE_PATH   : /data/workspace/.push-sync.json     ← A 的
        state_source : legacy
        ★ 期望（按注释「放进 .git/ 天然按仓库隔离」）
                     : /tmp/n99/.git/push-sync.json
        ★ 实际落在了 : /data/workspace/.push-sync.json

    `resolve_config` 的 `elif os.path.exists(old)` 命中 → **把 A 仓库的
    基线当成 B 仓库的基线**。

    后果链最要紧的一环：后面 `state.remote` 校验会拦下（fail-closed，
    方向对），但**它给的第二个补救选项是有破坏性的** ——
    `--root B --reset-baseline` 会 init_baseline() 写到 STATE_PATH，
    也就是用 B 的内容覆盖 A 的基线。用户照提示做，丢的是 A 的参照。

    修法：只有 ROOT 仍是编译期默认仓库时才认这份遗留文件。换了仓库，
    旧位置的那份就不再属于本次运行 —— 退回 `repo` 路径（不存在则新建）。

    与 N71（CONFLICT_DIR 由编译期常量改为跟随 ROOT）同类。
    """
    if os.path.abspath(ROOT) != os.path.abspath(_DEFAULT_ROOT):
        return None
    return _DEFAULT_STATE_PATH


_REPO_SLUG_PART = re.compile(r"^[A-Za-z0-9._-]+$")


def _parse_repo_slug(slug):
    """解析 `--repo` / `PUSH_API_REPO` 的**完整** slug → (owner, repo)。

    N419（第一百三十五轮，中危）：原实现是 `o, r = slug.split("/", 1)`
    —— 不校验、也不与 origin 比对。实测：

      --repo 'a/b/c'   → /repos/a/b/c
      --repo 'o/r/'    → /repos/o/r/
      --repo '  o/r  ' → /repos/  o/r
      --repo '../x/y'  → /repos/../x/y   ★ 请求发出时被归一化 → 打到
                                            另一个 API 端点

    返回 (None, None) 表示形态不合法，由调用方拦下（不猜）。
    """
    if not isinstance(slug, str):
        return None, None
    t = slug.strip()
    if t.count("/") != 1:
        return None, None
    o, r = t.split("/", 1)
    if not (_REPO_SLUG_PART.match(o) and _REPO_SLUG_PART.match(r)):
        return None, None
    return o, r


def resolve_config(opts):
    """按 CLI > 环境变量 > git 推断 > 编译期默认 的优先级定 ROOT / 仓库 / 状态文件。

    只覆盖**没被外部改过**的项（与 _DEFAULT_* 快照比对）。测试会直接给
    mod.ROOT / mod.STATE_PATH 赋值搭沙箱，那时必须让它们生效 —— 否则
    配置推断一跑就把沙箱冲了。

    返回解析结果的 dict，便于测试断言「优先级有没有生效」，也便于 --status
    把当前配置打印出来（配置错了是最难排查的一类问题，因为症状在远端）。
    """
    global ROOT, OWNER, REPO, BASE, STATE_PATH
    global _REPO_EXPLICIT
    info = {}

    # N417：同进程内第二次调用（脚本被当库调用 / 测试连跑两种配置）时，
    # 先把「上次我们自己写进去的」四项复位，否则它们会被判成
    # 「外部显式设过」而**永久残留** —— 实测连 detect_repo_slug 都跟着
    # 回答第一个仓库。`switch_repo()` 因为 _sig 由这四项的当前值算出，
    # 四项不变 → 连缓存都不清（N152 的修复在此场景完全不触发）。
    _pre = _snapshot_pre_resolve()
    _reset_previously_resolved()

    # ---- ROOT ----
    root = (opts or {}).get("root") or os.environ.get("PUSH_API_ROOT")
    if root:
        root = os.path.abspath(os.path.expanduser(root))
        info["root_source"] = "cli" if (opts or {}).get("root") else "env"
    else:
        root = _git_toplevel()
        info["root_source"] = "git" if root else "default"
    if not root and ROOT == _DEFAULT_ROOT:
        # 拿不到任何依据时回退 cwd，而不是编译期那个硬编码路径。
        #
        # 脚本是**单文件**形态（拷到目标仓库里用），新目录里常常既没有
        # git remote、也没有环境变量。此时沿用编译期默认值的话，ROOT 会
        # 指向写这份脚本时所在的那个仓库 —— 而当前目录跟它毫无关系。
        #
        # cwd 未必是仓库根，但它至少**与用户当前所在位置有关**，
        # 且配合下面的仓库硬校验，错了会被拦下而不是静默推到别处。
        root = os.getcwd()
        info["root_source"] = "cwd"
    if root and ROOT == _DEFAULT_ROOT:
        ROOT = root
    info["root"] = ROOT

    # ---- OWNER / REPO ----
    slug = (opts or {}).get("repo") or os.environ.get("PUSH_API_REPO")
    _slug_explicit = bool(slug)
    if slug:
        info["repo_source"] = "cli" if (opts or {}).get("repo") else "env"
        if "/" in slug:
            o, r = _parse_repo_slug(slug)
            if o is None:
                # 形态不对（多段 / 空段 / 路径穿越）→ 不猜、直接拦。
                # 实测 `--repo '../x/y'` 会拼成 /repos/../x/y，请求发出时
                # 被归一化 → 打到**另一个 API 端点**。
                _blocked(f"--repo 不是合法的 owner/repo：{slug!r}\n"
                         f"  需要形如 alice/myapp（两段，均不含 / 与空白）")
            else:
                # N419：完整 slug 分支**全程不看 origin 一眼**，而此刻
                # detect_repo_slug(ROOT) 明明知道正确答案。实测三种情况
                # （一致 / 打错一个字母 / 完全另一个仓库）**全部零提示**。
                #
                # N294 修的是裸仓库名这条入口（它会推到默认账号下），
                # 本条是同一个闸门的另一个入口。--repo 是用户显式指定、
                # 用户说了算，所以**不拦下，只提示**。
                go, gr = detect_repo_slug(ROOT)
                if go and gr and (go, gr) != (o, r) and not _json_active():
                    print(f"  ! --repo 指定的是 {o}/{r}，"
                          f"而本目录的 origin 是 {go}/{gr} —— 两者不一致。"
                          f"\n    确认这是你想推的目标"
                          f"（指定值生效，origin 仅供参考）。")
        else:
            # N294（第八十六轮，高危）：裸仓库名**只给了 repo**，
            # 原实现 `o, r = OWNER, slug` 让 OWNER 停在编译期默认值。
            #
            # 实测（新目录无 remote）：
            #   --repo my-project  → adwlachowicz684-star/my-project  拦下=False ❌
            # 更可达的一种：origin 明明是 alice/myapp，
            #   --repo myapp       → adwlachowicz684-star/myapp       拦下=False ❌
            #   detect_repo_slug 此刻能给出 ('alice','myapp')，却没被用上。
            #
            # 裸仓库名是 parse_args 明确支持的写法（报错文案原文
            # "--repo 需要 **owner/repo 或 repo** 参数"），所以不是
            # "用户输错了"，而是**脚本主动提供的一条入口**，而这条入口
            # 恰好绕过专为它设计的闸门。
            #
            # CI 里 `PUSH_API_REPO=$REPO_NAME` 是常见写法（很多 CI 变量
            # 只给仓库名不带 owner），所以这条不罕见。
            o, r = OWNER, slug
            if o == _DEFAULT_OWNER:
                # OWNER 没有真依据 → 先从 origin 取；取不到就当未解析，
                # 让 _refuse_default_repo 拦下（推到别人账号下最危险）。
                go, _gr = detect_repo_slug(ROOT)
                o = go if go else None
    else:
        o, r = detect_repo_slug(ROOT)
        info["repo_source"] = "git" if (o and r) else "default"
    # 用户真的要推的就是那个默认仓库时，`OWNER, REPO = o, r` **值没变**，
    # 于是「当前值 != 默认常量」这个判据会永远认为"你没指定" —— 死锁。
    # 所以显式记一个"有没有依据"，而不是靠值比较去反推。
    #
    # 注意 `o and r` 是**两个都要有依据**才置位 —— N294 的裸仓库名在
    # 拿不到 owner 时被置成 None，于是这里自然不置位 → 被拦下。
    if o and r:
        _REPO_EXPLICIT = True
    else:
        # M66（A74）：原先这里**没有 else** —— 只置 True，从不置 False。
        #
        # 而它是 `_refuse_default_repo` 的**唯一**放行判据
        # （`if _REPO_EXPLICIT: return`）。于是同进程内"上一次的仓库依据"
        # 会**残留**到这一次：
        #
        #   1 有 remote（reset 后）     True   → 放行 ✓
        #   2 无 remote（**不** reset） True   → 放行 ★
        #   3 无 remote（reset 后）     False  → 拒绝 ✓
        #
        # 场景 2 正是这道防护要防的事故：换到一个既无 `git remote` 也无
        # `--repo` 的仓库，却被放行 → 写操作落到**编译期默认仓库**
        # （docstring 原话：用户以为在推自己的仓库，实际推到别人仓库，
        #   本地全程正常）。
        #
        # 真实 CLI 是新进程（初始 False），所以**当前不可达** —— 补上
        # 不改变任何现有行为。可达的是脚本被当库调用 / 测试连跑两种配置
        # （`测试/test_config_guard.py` 就在同一进程里连调）。
        #
        # 旁边 `switch_repo` 那段注释写着"刻意不在这里清、由
        # reset_per_run_globals 负责"，而复位挂在 `main()` 的 finally ——
        # **不在「换仓库」这条路径上**。两处注释互相矛盾，
        # 而真正缺的就是这个 else：让「本次有没有依据」每次重新回答，
        # 而不是沿用上一次的答案。
        #
        # ⚠ 本条曾被 PR #217 整体覆盖而**静默回退**（A99 复核发现）。
        _REPO_EXPLICIT = False
    if o and r and (_slug_explicit
                   or (OWNER, REPO) == (_DEFAULT_OWNER, _DEFAULT_REPO)):
        # N417 附带：`--repo` / `PUSH_API_REPO` 是**本次显式给的依据**，
        # 它必须能覆盖「上次解析写进去的值」—— 否则同进程里第二次
        # `--repo` 会静默沿用第一次的仓库（实测：连打三次不同 slug，
        # 只有第一次生效）。外部（测试沙箱）直接给 mod.OWNER 赋值的情形
        # 不受影响：那种情况 `_slug_explicit` 为 False。
        OWNER, REPO = o, r
        BASE = f"https://api.github.com/repos/{OWNER}/{REPO}"
    info["repo"] = f"{OWNER}/{REPO}"

    # ---- STATE_PATH：--state 优先，其次仓库内，最后兼容旧全局路径 ----
    sp = (opts or {}).get("state_path")
    if sp:
        STATE_PATH = os.path.abspath(sp)
        info["state_source"] = "cli"
    elif STATE_PATH == _DEFAULT_STATE_PATH:
        # 默认位置从「仓库外的全局文件」改为 <root>/.git/push-sync.json。
        #
        # 仓库外那份是跨仓库串档的根源：推第二个仓库会静默共用同一份基线，
        # 现在靠 state.remote 事后校验兜住 —— 那是补救，不是设计。
        # 放进 .git/ 有三个好处：天然按仓库隔离、不入库（.git 不被 git 跟踪）、
        # 不污染工作区。
        new = os.path.join(ROOT, ".git", "push-sync.json")
        old = _legacy_state_path()
        if os.path.exists(new):
            STATE_PATH, info["state_source"] = new, "repo"
        elif old and os.path.exists(old):
            # 旧文件还在 → 继续用，并提示迁移。
            # 直接换路径会让老用户的基线「凭空消失」，表现为所有文件
            # 突然都变成「基线未记录」被拦下 —— 而改动其实一个都没丢。
            STATE_PATH, info["state_source"] = old, "legacy"
            info["state_hint"] = (
                f"沿用旧位置 {old}（检测到既有基线）。"
                f"迁移：cp {old} {new}")
            # N324（第九十九轮，中危）：legacy 判据只看「一个编译期写死的
            # 绝对路径上的文件在不在」—— 那个路径不含 ROOT、不含仓库名，
            # 与本次要推的仓库**毫无关系**。只要它存在（脚本自己旧版本
            # 写的），此后所有仓库都会退回共用同一份全局基线。
            #
            # 而这正是上方注释专门修掉的模式：「仓库外那份是跨仓库串档
            # 的根源」—— 仅因一个遗留文件就静默复活。
            #
            # 更麻烦的是静默程度：state_hint 只在 --status 的配置展示里
            # 打印，正常推送路径一声不响，用户看到的是「✅ 推送完成」。
            #
            # 诚实的限定：state.remote 校验会拦住最坏情况（A 的基线拿去
            # 推 B 会报 remote 不匹配并拒绝），所以**不是必然误推**。
            # 这里做的是把告知渠道补进推送路径 —— 用户至少知道自己在
            # 用一份可能与本仓库无关的基线。
            global _LEGACY_STATE_ACTIVE
            _LEGACY_STATE_ACTIVE = (old, new)
        else:
            STATE_PATH, info["state_source"] = new, "repo"
    else:
        info["state_source"] = "external"
    info["state_path"] = STATE_PATH
    # 是否仍在用编译期默认仓库。测试里 mod.OWNER/mod.REPO 被改过就不算，
    # 那时 (OWNER, REPO) != 默认值 —— 外部显式设过等同于有依据。
    info["repo_is_default"] = (OWNER, REPO) == (_DEFAULT_OWNER, _DEFAULT_REPO)

    # N152：`switch_repo()` 全文件**零调用点** —— 它是为「同一进程内
    # 连续推两个不同仓库」设计的（并行是两个进程，本就不共享内存；
    # 真正会串的是脚本被当库调用、或测试里连跑两种配置）。
    #
    # 零调用点的后果：Config 换了仓库，而 _RT 里还装着**上一个仓库**的
    # tree 缓存、探测结果、no_git 标志 ——
    # 拿 A 仓库的缓存去判 B 仓库，是静默错误（症状在远端，本地全绿）。
    #
    # 接在配置解析的出口：仓库/状态文件任一变化就作废派生状态。
    # N71：`_refresh_conflict_dir()` 定义了却**零调用点** ——
    # CONFLICT_DIR 由编译期 ROOT 算出后从不更新，于是用 --root 推另一个
    # 仓库时，冲突副产物（远端明文）仍写进**原来那个仓库旁边**的目录。
    # 这是判据 3 的第 6 个实例：修法写了，触发点没接。
    _refresh_conflict_dir()
    _sig = (OWNER, REPO, ROOT, STATE_PATH, BASE)
    if _sig != getattr(switch_repo, "_last_sig", None):
        switch_repo._last_sig = _sig
        switch_repo()
        info["runtime_reset"] = True
    else:
        info["runtime_reset"] = False
    # N417：记下本次写入的值，供下次调用区分「外部设的」与「我们设的」。
    # ⚠ 必须在**函数末尾**（STATE_PATH 定完之后）—— 我第一版放在
    # `info["repo"]` 之后，那时 STATE_PATH 还没写，于是记下的是默认值，
    # 下次调用判定「当前值 != 记录值」→ 不复位 → STATE_PATH 照样残留。
    for _name, _dflt in _DEFAULT_OF.items():
        _after = globals()[_name]
        if _after != _pre.get(_name):
            # 只登记「我们真的写了」的项，并记下**写之前它是不是默认值**。
            _LAST_RESOLVED_VALUES[_name] = (_after, _pre.get(_name) == _dflt)
    return info


#: 只读子命令：它们不打远端写请求，配置不全时不该被仓库校验拦下。
#: --scope 与 --status 同理：只做读与分类，不落盘、不碰远端。
READONLY_OPTS = ("status", "prune", "scope")
# N418（第一百三十五轮，中危）：这份名单**漏了四个纯本地命令**，于是
# "没有 origin 的新目录"里（脚本单文件形态下最常见的场景）：
#   --audit / --clean-conflicts / --resolve / --mark-synced → 被拦下(exit 2)
# 而这四个**一个都不碰远端**：
#   --audit            只读本地 .git/push-audit.jsonl
#   --clean-conflicts  只删本地副产物目录
#   --resolve          只清本地 state 里的冲突标记
#   --mark-synced      只写本地状态文件
# 同时名单内放行的 --status / --scope 恰恰都要打远端 —— 名单内外各有一半
# 是错的，而它们读的是编译期写死的默认仓库这件事本身没有提示。
#
# `--audit` 最该修：脚本自述「排查『上周那次到底推上去没有』**只能靠**
# 它」，而"没有 origin 的新目录"正是最需要排查的时刻之一 —— 唯一的
# 痕迹查看入口自己先拒绝工作。
#
# 与 N314 / N336 / N299 同族（同一份"纯本地命令"事实在另一道闸门上没同步）。
_PURE_LOCAL_OPTS = ("audit", "clean_conflicts", "resolve", "mark_synced")
READONLY_OPTS = tuple(list(READONLY_OPTS) + list(_PURE_LOCAL_OPTS))


def _refuse_default_repo(opts=None, action="执行"):
    """仓库仍是编译期默认值时，拒绝一切会打到远端的写操作。

    这条要防的是本工具最难排查的一类事故：**症状全在远端**。
    脚本是单文件形态（拷进目标仓库用），新目录里往往既无 git remote
    也无环境变量，于是 OWNER/REPO 悄悄落到编译期写死的那个仓库 ——
    用户以为在推自己的仓库，实际把内容推到了别人（或自己另一个）仓库里，
    而本地全过程看起来完全正常，连 ✅ 都照打。

    阶段 0 的 detect_repo_slug() 只覆盖了「有 git remote」这一种；
    恰恰是**没有** remote 的新目录（最该警惕的场景）它帮不上忙。

    为什么不在 resolve_config 里直接抛：--status / --prune 是只读的，
    它们正是用户用来**确认配置对不对**的手段 —— 先拦住就等于让人在
    黑箱里排错。只读放行、写操作拦下。
    """
    # 判据是「有没有依据」，不是「值不等于默认值」。
    # 后者会把**显式指定默认仓库本身**判成"没指定" —— 用户照提示给了
    # --repo adwlachowicz684-star/tauriTools，仍然被拒，且提示让他再指定
    # 一次同一个值，形成死锁（实测确认）。
    #
    # 唯一的绕法是改本地那份脚本的编译期常量，那是拿"改代码"换"能运行"，
    # 不该是正常用法。
    if _REPO_EXPLICIT:
        return
    if (OWNER, REPO) != (_DEFAULT_OWNER, _DEFAULT_REPO):
        return
    opts = opts or {}
    if any(opts.get(k) for k in READONLY_OPTS):
        return
    _blocked(
        f"无法确定目标仓库，拒绝{action}。\n"
        f"  当前仍是编译期默认值 {OWNER}/{REPO}，"
        f"推上去会落到**另一个仓库**，而本地看不出任何异常。\n"
        f"  三选一：\n"
        f"    python3 push_api.py --repo owner/repo ...\n"
        f"    export PUSH_API_REPO=owner/repo\n"
        f"    git remote add origin <仓库地址>     # 之后自动推断\n"
        f"  先看配置：python3 push_api.py --status")


def _apply_state_path(opts):
    """让 --state 生效：替换全局 STATE_PATH。

    必须在**任何**读写基线之前调用：save_state / load_state 都直接读
    全局 STATE_PATH，晚一步就会先动默认路径上的文件。

    抽成函数是为了能单独测 —— 子进程跑真实命令会碰网络，
    而这里要验的是「参数有没有真的作用到全局」这一步。
    """
    if not opts.get("state_path"):
        return
    global STATE_PATH
    STATE_PATH = os.path.abspath(opts["state_path"])


def main():
    # per-run 全局在**运行结束时统一复位**（finally，覆盖正常返回 /
    # SystemExit / 异常三条出口）。
    #
    # 为什么必须在出口复位而不是只靠入口赋值：
    # 真实 CLI 是新进程，无所谓；但**测试在同一进程里连续跑多个用例**，
    # 前一个用例的 --dry-run 会残留到后一个直接调 save_state 的用例上，
    # 表现为「未抛异常」—— 测试红，而根因是全局串味，极难定位。
    # 让 per-run 标志名副其实（一次运行内有效），这类串档一次性消失。
    try:
        return _main_wrapped()
    finally:
        # 注意：复位只在**末尾**做一次。开头若也调一次，
        # 会把本次刚置位的 _JSON_EMITTED 清掉，导致 --status --json
        # 输出两份（实测）。per-run 状态就该"用完再清"。
        # N136：#31 修 N112（set_ui 生产路径零调用）时引入了回归 ——
        # json 模式下 Ui.line() 只把内容收进 events、**不 print**，
        # 落地靠 Ui.flush()，而 flush 在生产路径同样零调用。
        # 于是 --json 模式下 48 处防护指引全部蒸发：
        # 用户拿到一个 events 为空（或残缺）的 JSON，而文本模式正常。
        # 「声称支持 --json 却什么都不输出」比不支持更糟。
        #
        # 唯一的正确出口就是这里：所有 return / raise 都经过 finally。
        try:
            # N149：无条件 flush 是错的。全脚本 print() 有 467 处、
            # _UI.* 只有 48 处（且集中在 RewindGuard）——
            # 除 --status 外，任何子命令的 --json 都会得到
            #「前面一坨文本 + 末尾一个残缺 JSON」。
            # 半结构化输出比没有更危险：人会拿它当可靠依据
            #（show_status docstring 原话）。
            #
            # 所以：只有本次确实走了 Ui 通道（有事件）时才落地；
            # 否则保持纯文本。--json 的适用范围由各子命令自己声明，
            # 与 --prune 已有的「暂不支持 --json」口径一致。
            _u = get_ui()
            if _u.mode == "json" and _u.events and not _JSON_EMITTED:
                _u.flush()
            elif (_u.mode == "json" and _u.json_warned and not _JSON_EMITTED
                  and _DISPATCH_REACHED and not _JSON_UNSUPPORTED_WARNED
                  and not _STREAM_MODE):
                # M80：加 `_DISPATCH_REACHED`。缺 token / 默认仓库拒绝 /
                # 无基线 / 取不到 ref 都在分派**之前** return，那时说
                # "本子命令暂不支持 --json" 是**假的** —— 真原因要么在
                # stderr，要么刚以 JSON 形式输出过。
                #
                # N531（第一百七十四轮，中危）：再加 `_STREAM_MODE`
                #（--mine 纯数据流）。实测决定性对照：
                #     --scope --json        → 合法 JSON（--scope 支持 json）
                #     --scope --mine --json → 「本子命令暂不支持 --json」★
                # 后一句是**假的**：不支持 json 的不是 --scope，而是
                # show_scope 内部的 only_mine 分支（它排在 `if as_json:`
                # 之前就 return 了）。而 finally 按"进了分派区 + 是 json
                # + 没输出过 JSON"判据成立就打 —— 于是说的是"本子命令"，
                # 恰恰是本子命令支持的那一个。消费者照它去改命令，会以为
                # 该换成 --status --json，实际该做的是去掉 --mine。
                #
                # 更实际的一层：这句人类文案会混进 --mine 的管道被当成
                # 文件名去推，正是 show_scope docstring 明令禁止的事
                #（`| while read f` 消费）。从"show_scope 内部多加一句"
                # 换成"finally 多加一句"，禁止的对象没变。
                #
                # 不改成"给 --mine 补一份 JSON"：--mine 的契约是纯数据流
                #（一行一个路径），改成 JSON 会破坏既有消费者。
                #
                # N640（A139-M148，低危）：括号里原先只提 --status，而
                # --scope --json **实测输出合法 JSON**（12 个键：base_commit
                # / base_entries / counts / files / head / hints / ok /
                # pushable / rewound / synced_commit / warnings / base…），
                # 且分派区注释自己写着「--scope 与 --status 同构」。
                #
                # 后果是 AI/CI 读到这句会以为 --scope 也没有结构化输出而
                # 不用它 —— 而 --scope 恰恰是**待推送清单**的出口，比
                # --status 更贴近推送决策，被低估的正是最该用的那个。
                # 三处（本处 / _warn_json_unsupported 默认 msg / --prune
                # 专用句）与 --help（多「完整」二字）一并改。
                print("  ! 本子命令暂不支持 --json，按文本输出"
                      "（结构化输出目前只在 --status / --scope 上可用）")
        except BaseException:
            pass
        # M152（A142，中危）：`_warn_mode` 在 json 模式下把安全警告**收起来**，
        # 交给下游 payload 的 `warnings`。收起的前提是"下游一定有人接"，
        # 而全文件**只有一个消费者**（`show_status` 并入 payload）——
        # 更狠的一半是：`--mark-synced` / `--pull` / `--audit` 这类命令
        # 压根没有 json 出口，收起后**文本与 JSON 两个渠道同时没有**
        #（A142 §2.2 逐个真跑的表：10 个子命令里 4 个真有 json 出口、
        # 其中 3 个拿不到警告；另 6 个连 json 都没有）。
        #
        # 收起的是**安全警告**（状态文件与远端明文副本别人可读），而脚本
        # 自己的注释点名 CI 跑在 virtiofs / 容器挂载上、是**最容易命中**
        # 的一类环境。没人接就等于删掉。
        #
        # 收起发生在分派**之前**（load_state → _harden_once），那时还不知道
        # 本命令有没有 json 出口 —— 所以在运行出口补一道：本轮**根本没输出
        # 过 json**，就把攒下的警告照常打到 stderr（不进 stdout，不打断解析）。
        try:
            if _JSON_PENDING_WARNINGS and not _JSON_EMITTED:
                for _w in list(_JSON_PENDING_WARNINGS):
                    print(_w, file=sys.stderr)
        except BaseException:
            pass
        # N240：复位会把 _BLOCKED_BY_GUARD 清成 None，而顶层的
        # `if _BLOCKED_BY_GUARD: exit(2)` 在 main() **之后**才判 ——
        # 于是被 return 型 Guard（RewindGuard / StaleLocalGuard）拦下时
        # 退出码恒为 0，CI 把「被拦下」当成推送成功。
        #
        # M42（A44/A46/A50，P1）：N240 当初的写法是「复位**之前**把值
        # 转移过去」，并假设它此后不会被复位清掉。
        # 但后来判据 5（per-run 全局必须复位）把它扫了出来并补进复位
        # 清单 —— 那个"此后不会被清"的前提就失效了：
        #     转移 → _LAST_RUN_BLOCKED = 'stale_local'
        #     复位 → _LAST_RUN_BLOCKED = None      ← 紧随其后
        # 顶层恒读到 None，退出码恒 0 —— N240 等于没修。
        #
        # 修法：**先取值、再复位、最后写回**。
        #   · 取值必须在复位前（_BLOCKED_BY_GUARD 会被清）
        #   · 写回必须在复位后（否则立刻被清）
        # 它确实在复位清单里（这条不能改：跨运行串味会让上一次
        # 被拦下的结果污染下一次成功运行，退出码假报 2）。
        global _LAST_RUN_BLOCKED
        # N362：取消标志与上面完全同族，必须**同一套三步**处理。
        # 我第一版只在 _Cancel 处置位就完事 —— 实测退出码仍是 0，
        # 因为 main() 的 finally 里 reset 会把它清掉，而顶层判据在
        # main() **之后**。与 M42 一模一样的坑，我踩了第二次。
        global _LAST_RUN_CANCELLED
        # M126：同样必须 global。我第一版漏了这句，实测
        # `UnboundLocalError: local variable '_LAST_TERM_SIGNAL'
        # referenced before assignment` —— 因为块末尾有**赋值**
        #（`_LAST_TERM_SIGNAL = _sig_now`），Python 于是把整个名字
        # 当成局部变量，连前面的**读**也一起判成未绑定。
        # 而它发生在 finally 里，会盖掉真正的终止原因 —— 用户看到
        # 一个 UnboundLocalError 而不是「已被 SIGTERM 终止」。
        global _LAST_TERM_SIGNAL
        _cancelled_now = _LAST_RUN_CANCELLED
        _blocked_now = _BLOCKED_BY_GUARD
        # M126：终止信号同族 —— 它在复位清单里（跨运行不串味所必需），
        # 所以同样必须"先取值 → 再复位 → 最后写回"。少这一步，顶层恒
        # 读到 0 → CI 超时被杀时用户看到的是"已中断（Ctrl-C）"。
        _sig_now = _LAST_TERM_SIGNAL
        # N542：json 命令里没有 warnings 字段的那些出口（--mark-synced
        # --json / --pull --json 等），收起来的警告到这一步仍没人取 ——
        # 再往下 reset_per_run_globals() 就把它们清掉了，永久消失。
        # 兜底写 stderr：不进 stdout，JSON 照样可解析，终端上照样可见。
        try:
            _left = _drain_json_warnings()
        except BaseException:
            _left = []
        if _left:
            sys.stderr.write("\n  ! 本轮有 %d 条警告（json 模式下未进 "
                             "payload）：\n" % len(_left))
            for _l in _left:
                sys.stderr.write("     - %s\n" % _l)
        reset_per_run_globals()
        if _blocked_now:
            _LAST_RUN_BLOCKED = _blocked_now
        if _cancelled_now:
            _LAST_RUN_CANCELLED = _cancelled_now
        if _sig_now:
            _LAST_TERM_SIGNAL = _sig_now


def _main_wrapped():
    opts, explicit = parse_args(sys.argv[1:])
    # N480：尽早装 —— 从这一刻起，SIGTERM / SIGHUP 也走清理通道。
    # 放在 parse_args 之后、resolve_config 之前：晚一步的话，"解析参数
    # 期间被取消"这段窗口仍会留下 PAT 明文临时文件，而那恰恰是 CI
    # 最容易打断的时刻（超时杀进程）。
    _install_term_handlers()
    # M125：报残留。放在分派之前，但纯数据流模式下**改走 json 通道** ——
    # 否则 `--status --json` 的首行是这几行文本，整体不可解析而 rc=0
    #（与 N99 / M81 / M114 同族的判据：分派前的输出必须静音）。
    #
    # N585（A154，中）：上一版是「静音」而不是「换通道」—— json 模式下
    # 这几行直接不打，`warnings` 字段（运载体就在 145 行外的
    # `_json_pending_warnings()`，消费者是 `show_status`）**永远为空**。
    # 于是「残留里有你的 PAT 明文」这件事在 AI/CI 唯一会读的那个出口上
    # 一次都看不到，而它恰恰是这条告警唯一的设计目标。
    # 现在：json 模式下收进 warnings 字段；无该字段的出口（`--mine` 等
    # 纯数据流）在分派后由 `_drain_json_warnings()` 兜底写 stderr。
    _stream = bool(opts.get("json") or opts.get("mine"))
    try:
        _report_stale_auth_files(stream=_stream)
    except KeyboardInterrupt:
        # M170（A154，中）：`KeyboardInterrupt` 是 `BaseException` 的子类，
        # 而信号 handler（`_install_term_handlers`）抛的正是它 —— 就装在
        # 上面 5 行。包在 `except BaseException: pass` 里意味着：SIGTERM
        # 恰好落在这段窗口内时，脚本既不退出、**这一次的残留报告也整个
        # 消失**，跑完整轮后退出 0。
        #
        # 与 N639（A135，/user 探测那处）是同一个洞的两处实例。凡是装了
        # handler 之后的路径上出现 `except BaseException`（而不重抛 KI），
        # 都是这个洞。
        raise
    except BaseException:
        # 扫描是告知性动作，绝不许它把主流程带崩。
        pass
    # 必须在**任何**读 ROOT / STATE_PATH 之前调用：StateLock 要用 STATE_PATH，
    # 而它的默认值（仓库内 .git/）取决于解析出来的 ROOT。
    # 晚一步就会拿编译期那个硬编码仓库去加锁 —— 症状是「推 A 仓库却锁了
    # B 仓库的状态文件」，且几乎无法从输出看出。
    cfg = resolve_config(opts)
    # 存一份供 --status 展示：配置错了是最难排查的一类问题 ——
    # 症状表现在**远端**（内容推到了别处），而本地一切看起来都正常。
    # 让用户能一眼看到「这次到底在对哪个仓库操作」。
    global _CONFIG_INFO
    _CONFIG_INFO = cfg
    _apply_state_path(opts)
    # 仓库仍是编译期默认值时拦下写操作。放在 resolve_config 之后：
    # 只有它跑完才知道「推断有没有成功」。
    _refuse_default_repo(opts)
    # --api：本次禁用 git 通道（用户显式选择）
    if opts.get("api"):
        _RT.no_git = True
    # 锁**不能**用 dry 推断：dry 时子命令照样会写状态文件（上面已拦住大部分，
    # 但门禁本身也可能有漏），更关键的是「可能写」就该加锁，
    # 用「这次不是 dry」来推断会不会写，等于把并发安全寄托在另一处的判断上。
    # M65：纯数据流模式下把锁的提示静音（N99 判据：分派前不输出）。
    with StateLock(STATE_PATH, enabled=True,
                   quiet=bool(opts.get("json") or opts.get("mine"))):
        try:
            _main(opts, explicit)
        except _Cancel:
            # 分支创建**之前**的取消：那时还没有东西要清理，
            # 但异常不能冒到用户面前 —— 取消是正常流程，不是错误。
            #
            # M71（A77）：N362 修的是 `_main` **内部**那一处（L12205 附近），
            # 这一处漏了 —— 于是同一件事有两条出口、两个退出码：
            #
            #     _main 内 except _Cancel     → 置位 → 退出码 4 ✓
            #     这里（_main_wrapped）        → 只有 pass → 退出码 0 ★
            #
            # 报告实测：让 _Cancel 从 _main 逃出时 exit=0，而 CI 把 0 读成
            # "推送成功"，可远端一个字节没变。
            #
            # 当前这条路径**不可达**（9 处 raise _Cancel 都在 _main 的 try 内），
            # 所以补上不改变任何现有行为 —— 代价为零。
            # 但不能不补：只要有人在分支创建前新增任何一个取消点
            #（比如给 PRE_PUSH_GUARDS 加一道 action="cancel" 的 Guard），
            # 就立刻退回退出码 0，而 N362 那套修法会被认为已经封住了。
            # 这正是"防复发机制只覆盖了当时注意到的那条路径"。
            global _LAST_RUN_CANCELLED
            # M119①：无事可做走的是同一条 _Cancel 通道（仅为触发分支清理），
            # 但它不是「用户没确认」—— 不置位，退出码回到 0。
            if not _NOTHING_TO_PUSH:
                _LAST_RUN_CANCELLED = True
        except BaseException as _we:
            # N383（第一百二十三轮，中危）：`_main()` 里「失败即清理」的
            # try 在**本地补提交结束**就合上了，而之后还有三步在 try 之外：
            #
            #     save_state()   ← 磁盘满 / 只读挂载 → OSError
            #     ensure_pr()    ← 有自己的 try，只打印不审计
            #     do_merge()     ← 返回假时 return（N376）
            #
            # 此时远端分支上**已经有本次全部内容**。实测（注入 save_state
            # 抛 OSError）：audit(push_fail) 零次、_cleanup_failed_branch
            # 够不到、OSError 不是 SystemExit 于是顶层不接 → 裸 traceback。
            #
            # 同一个"失败"语义，跨过那一行就完全变了样 —— 而 audit 的
            # docstring 写的存在理由正是「事后要能回答那次到底推到哪一步」。
            #
            # 这里补三件事，不动退出码语义（仍原样抛出）：
            #   ① 留凭据（push_fail）—— _main 内那一处已记过则跳过，去重靠
            #      _PUSH_FAIL_AUDITED；
            #   ② 明说内容已推到哪个 ref；
            #   ③ **不做清理** —— 分支上有内容，删掉就是删用户刚推的改动
            #      （P1-8 那段论证过：那时该提示，不该悄悄删）。
            global _PUSH_FAIL_AUDITED
            if not _PUSH_FAIL_AUDITED:
                _PUSH_FAIL_AUDITED = True
                try:
                    audit({"kind": "push_fail",
                           "branch": _LAST_TARGET_REF,
                           "err": f"{type(_we).__name__}: {_we}"[:200],
                           # 有意不清理（上面 ③），所以恒 False。
                           "cleaned": False})
                except BaseException:
                    pass
            if _LAST_TARGET_REF:
                print(f"\n  ! 本次内容**已推到远端** {_LAST_TARGET_REF}，"
                      f"但收尾步骤未完成（{type(_we).__name__}）。")
                print("     分支与内容都还在，改动没丢。"
                      "请勿 --delete-branch，重跑或用 --merge 继续即可。")
            raise
        finally:
            # 待删除清单的统一出口。
            #
            # 放在这里而不是流程中段：中段后面还有几十行推送输出，
            # 清单会被冲走；而放在每个 return 之前又会打很多遍。
            # 出口是唯一"既在最后、又只有一次"的位置，
            # 且能覆盖所有提前返回（含"没有待推送改动"那一条）。
            # N509（第一百六十六轮，低危）：下面只读 _RUN_DELETED_PATHS，
            # 从不赋值 —— 读全局变量不需要 `global`，写上去 pyflakes 会报
            # unused。`2ddbb8927`（chore）正是据此删掉了它，
            # 而 A7 那批提交从旧基线 squash 回来时把它**原样带回来了**。
            # 危害是纯告警回归（所以低危），但值得记一笔：
            # 这是「旧基线覆盖回新修复」的第 4 次（前三次都造成实质回退，
            # 这次只是运气好——那行没有 N/M/A 编号锚点，谁也不会去查）。
            global _RUN_PENDING_DELETES
            if _RUN_PENDING_DELETES:
                # N175：出口在 finally 里，必然在 _main() 返回**之后**
                # 执行，而它打印的是推送**前**登记的清单。
                # 删除开启且本次确实删掉了文件时，文件已从远端消失，
                # 出口却仍说"本次推送会真的删除上述路径"——
                # 用户按它去核查，会发现文件根本不在了，
                # 于是怀疑脚本没删成功，白排查一轮。
                #
                # 出口位于推送之后，必须**重新计算**而不是复读旧数据。
                # 拿不到（网络/状态异常）时才回退到登记值 ——
                # 播报"可能已删"比"确定要删"安全。
                _fresh = None
                try:
                    # N246：出口无条件发真实网络请求重算，
                    # **连 --dry-run 也发**。
                    # 而 dry-run 的契约是"不产生任何副作用"：
                    # 它常被用来在弱网/离线环境里先看一眼，
                    # 为一份提示文案发请求，可能把用户卡在超时上。
                    #
                    # dry 时直接用登记值，不发请求。
                    if _dry_this_run():
                        raise RuntimeError("dry-run：不重算")
                    _rf = api("GET", _ref_path(BRANCH)) or {}
                    _rh = (_rf.get("object") or {}).get("sha") or ""
                    # N306：同样改用 A∪B 并集（与登记处同一函数）。
                    # 重算是出口打印的**最终依据**，只用 A 会把
                    # 未 commit 的删除整类漏掉 —— 而那恰恰是
                    # _detect_deletions 存在的唯一理由。
                    _fresh = list(pending_deletes_union(
                        load_state() or {}, local_state_map(),
                        remote_state_map(_rh)) or [])
                except BaseException:
                    _fresh = None
                # N217：重算失败回退登记值时，登记处用的是 judged=False
                #（判据不完整，不能断言"会删除"），而这里默认 judged=True
                # —— 回退后反而做出**更强的断言**，与 N171/N162 同一类错误。
                # 回退时显式带 judged=False。
                #
                # N218：重算结果为空时什么都不打印 —— 而空意味着
                # 「删除已生效、远端已清干净」，恰恰是**最该说一句**的时刻。
                # 用户视角：文件删了，屏幕上没有任何反馈，
                # 唯一的告知渠道在成功时反而关闭。
                if _fresh is not None:
                    if _fresh:
                        # N246：dry-run 没有真正推送，"本次运行"（隐含
                        # 已推送）是错的语境 —— 会让文案里的
                        # "本次推送会真的删除"在预演中也出现。
                        report_pending_deletes(_fresh, "本次运行")
                    elif _RUN_PENDING_DELETES:
                        # N382（第一百二十二轮，中危）：先问**执行侧**。
                        # 删除已生效时 _fresh 必然为空（文件已从远端消失），
                        # 若直接落到下面那句「未被处理」，同一份输出里
                        # 上句「将删除 1 个」与下句「未被处理」自相矛盾，
                        # 而实际已经删了 —— 用户无从判断到底删没删。
                        if _RUN_DELETED_PATHS:
                            print("\n  · 本次已实际删除 "
                                  f"{len(_RUN_DELETED_PATHS)} 个远端文件：")
                            for _p in _RUN_DELETED_PATHS[:20]:
                                print(f"     - {_p}")
                            if len(_RUN_DELETED_PATHS) > 20:
                                print("       … 其余 "
                                      f"{len(_RUN_DELETED_PATHS) - 20} 个")
                            print("     删除不可逆；若属误删，可用 git 历史"
                                  "或远端提交记录找回，本脚本无回收站。")
                            _RUN_PENDING_DELETES = None
                        else:
                            # N234：这里原写「已删除 N 个文件，远端已无残留」——
                            # 是**假声明**。_fresh 为空只能说明「按**当前**
                            # 待删除判据算不出条目」，原因至少有三种：
                            #   1. 删除确实生效了（原推断）
                            #   2. 本次根本没走删除路径（功能关闭、非推送子命令）
                            #   3. 判据口径不同 —— _RUN_PENDING_DELETES 来自
                            #      baseline_ghosts（未查 local_seen），
                            #      _fresh 来自 pending_deletions（查了），
                            #      后者更严，于是登记非空而重算为空
                            # 第 3 种最常见，而「已删除」在那种情况下是谎话：
                            # 用户会以为文件已清掉，实际还留在远端。
                            #
                            # 改成只陈事实（登记过 N 条 + 现已不在待删清单里），
                            # 不推断「已删除」。
                            print("\n  · 本次运行登记过 "
                                  f"{len(_RUN_PENDING_DELETES)} 个待删除路径；"
                                  "按当前判据它们已不在待删除清单中"
                                  "（删除功能关闭时表示未被处理）。")
                elif _RUN_PENDING_DELETES:
                    report_pending_deletes(_RUN_PENDING_DELETES, "本次运行",
                                           judged=False)
                _RUN_PENDING_DELETES = None


# ==================== Runtime：可变状态的生命周期（阶段 3a）====================
#
# 背景：三个模块级缓存在 4 处手动 `global` 失效，已经管不住。
# 证据（代码注释自己承认的）：
#   · 6111 的 global 声明写在**嵌套块中间** —— 因为下面两处都要赋值，
#     写在赋值点前面会撞 SyntaxWarning。注释原文：「把正确性押在语句顺序上
#     是脆弱的隐式依赖」。
#   · 2652 的失效靠「每个调用点自觉传 refresh=True」，注释原文：「太脆弱，
#     这里从源头失效掉」。而源头失效正是靠 global 手动赋值。
#
# 审查清单 P0-3 给的验收标准：
#   「同一台机器并行推两个不同仓库互不干扰；grep 不到模块级可变全局。」
#
# 阶段 3a 只做**收敛**，不做注入：
#   · 三个缓存收进 Runtime 实例，有明确的边界与统一的失效入口
#   · 模块级保留一个单例 _RT，所有现有引用点行为不变
#   · 新增 switch_repo()：切仓库时一次性清掉全部派生状态
#
# 为什么不全量改成显式传递：那要动每一个调用点（detect_changes 有 20+ 处
# 调用、local_state_map 同理），一次改完没法逐步验证。先让边界**存在**，
# 阶段 3b 再沿这个边界把参数传进去 —— 那时每改一处都能立刻跑回归。


class Runtime:
    """一次运行 / 一个仓库的派生状态。

    与 Config 的区别：
      Config   输入的**事实**（仓库是谁、根目录在哪、基线文件在哪）
      Runtime  从这些事实**派生**出来的缓存与探测结果

    两者分开的意义：切仓库时 Config 整个换掉，Runtime 整个失效；
    而同一 Config 下可以反复复用 Runtime（一次推送里 detect_changes
    会被调很多次，不能每次都重新跑 git）。
    """

    __slots__ = ("tree_cache", "changes_cache", "exec_reliable",
                 "transport_probe", "no_git")

    def __init__(self):
        self.tree_cache = {}       # ref_sha -> 远端文件树
        self.changes_cache = None  # detect_changes() 结果
        self.exec_reliable = None  # 执行位是否可信的探测结果（三态：None 未探）
        self.transport_probe = None
        #: 本次运行是否禁用 git 通道（--api，或 git 推送失败后回退）
        self.no_git = False

    def invalidate_changes(self):
        """工作区被改写后调用。

        这是原来那个 global 声明落在嵌套块中间的地方：不清的话，
        后续任何 detect_changes()（无 refresh）都会返回**写入前**的
        旧快照，把刚落盘的改动当成「本地无改动」。
        """
        self.changes_cache = None

    def reset(self):
        """切仓库 / 换根节点时全部作废。**唯一**的作废入口。

        不清 tree_cache 的后果最隐蔽：它是按 ref_sha 索引的，切到另一个
        仓库后如果 ref_sha 恰好撞上（都是初始提交很常见），会直接返回
        上一个仓库的树 —— 于是推送"成功"却推了别人的内容。

        M33（A22）：本方法原先**零调用**，且只清 4/6 字段 ——
        而 `switch_repo()` 用 `_RT = Runtime()` 新建实例，
        两者是**同一件事的两份实现**（A22 原话）。
        两份实现的危险在于：哪天有人改用 `reset()`，
        它会漏掉 2 个字段 —— 正是 N204 修过的"跨仓库串味"症状。

        所以收敛成一份：`reset()` 清**全部**字段（含 no_git、
        及已删除的死字段 ui），`switch_repo()` 调它而不是新建实例。
        顺带修掉 ui 死字段：N112 之后 Ui 由模块级 _UI 持有，
        Runtime.ui 从未被赋值过。
        """
        self.tree_cache = {}
        self.changes_cache = None
        self.exec_reliable = None
        self.transport_probe = None
        self.no_git = False


_RT = Runtime()


def rt():
    """当前 Runtime 单例。

    M27（A21）：生产代码里零调用点 —— 因为 push_api.py 是脚本，
    内部直接用模块级 `_RT`。但**测试**依赖它（`mod.rt().exec_reliable`），
    所以它不是死代码，而是**面向测试的访问器**。

    刻意保留而不是删掉：删了测试就只能访问 `_RT` 这个私有名，
    把"测试专用通道"变成"外部依赖内部实现"。
    这里明确登记它的用途，避免下一个人又当成死函数删一遍。
    """
    return _RT


def switch_repo():
    """切换仓库：作废全部派生状态，返回新的 Runtime。

    审查清单的验收之一是「同一台机器并行推两个不同仓库互不干扰」。
    并行是两个进程，本来就不共享内存 —— 真正会串的是**同一进程内**
    连续推两个仓库（脚本被当库调用、或测试里连跑两种配置）。
    那种情况 Config 变了而缓存没清，是静默错误。

    A25 的归属核查查出：本函数被登记为 6 个模块级名字的归属者，
    实际只重建了 _RT 一个 —— 登记表退化成"我以为有人管"的注释。
    核查过程中还带出 N71 的 `_refresh_conflict_dir()` **零调用点**
    （判据 3 的第 6 个实例）：N71 写了修复函数，却没人调它。

    ⚠ 这里**只作废缓存**，不碰 BASE / ROOT / STATE_PATH：
    它们是本函数的**输入**（`_sig` 就是由它们算出来的），
    在这里清掉等于把刚解析好的配置抹了 ——
    实测：清了之后 StateLock 拿到 STATE_PATH=None，启动即崩。

    归类要按"谁产生它"而不是"它是不是模块级"：
      · BASE / ROOT / STATE_PATH —— 配置**输入**，由配置解析产生
      · CONFLICT_DIR             —— 派生值，由 _refresh_conflict_dir 跟着 ROOT 重算
      · _RT / _SUBMODULE_CACHE / _CONFIG_INFO —— 缓存，本函数作废
    """
    global _RT, _SUBMODULE_CACHE, _CONFIG_INFO
    # 刻意**不在这里**清 _REPO_EXPLICIT：它记录的是"本次仓库有没有依据"，
    # 由 resolve_config 置位 —— 而本函数正是被 resolve_config **调用**的
    # （签名变化时）。在这里清掉等于刚置上就被抹掉。
    #
    # 这正是上方那段注释自己写过的教训：「归类要按谁产生它」。
    # 它是配置解析的**产物**，不是本函数该作废的派生缓存。
    # 跨运行/跨仓库的复位由 reset_per_run_globals 负责。
    #
    # M33：原先是 `_RT = Runtime()` —— 与 `Runtime.reset()` 是
    # 同一件事的两份实现。改用 reset()：**同一实例**作废，
    # 持有旧引用的调用方也能看到新状态（新建实例做不到这点）。
    #
    # M40（A54）：M33 当时**只改了注释没改代码** —— 这里仍写着
    # `_RT = Runtime()`，于是上方这段注释描述的好处**一个都没兑现**。
    # 实测（在旧引用上塞一个键再切仓库）：
    #     返回的是新实例 : True
    #     旧引用仍带脏缓存: True        ← tree_cache['fake'] 残留
    # 后果正是 N204 修过的「跨仓库串味」：切到另一个仓库后若 ref_sha
    # 恰好撞上，持有旧引用的调用方会拿到**上一个仓库**的树。
    # 而新建实例后再 reset() 是双重作废 —— reset 打在刚造出来的空
    # 实例上，谁都看不见它。
    _RT.reset()
    _SUBMODULE_CACHE = None
    _CONFIG_INFO = {}
    _refresh_conflict_dir()
    return _RT

# ==================== Ui：统一输出出口（阶段 2）====================

def _content_removal_report(remote_map, paths, limit=40):
    """N397：本次推送会从远端**移除多少内容** —— 让静默覆盖变成可见。

    起因（第一百二十九轮实测）：两个会话并发编辑同一个 push_api.py，
    后推的一方把前者的修复**逐处删除**，而屏幕上只有

        ✅ 已合并进 main 并删除分支 task/...

    没有任何一行提示"本次从远端移除了 N 处改动"。撤销是静默的：
    不看 diff 根本发现不了。

    这不是要拦下推送（那会误伤正常重构），而是**让它可见** ——
    被移除的行会打印出来，任一方扫一眼就能发现"我的东西没了"。

    返回 (净移除行数, 被移除的行样本)。取不到远端内容时返回 None
    （不猜，也不因此拦下推送）。
    """
    if not remote_map or not paths:
        return None
    removed, sample = 0, []
    # M97：上限见 REMOVAL_CHECK_CAP 的注释（与 8383 行 PREVIEW_CAP 同一理由）。
    for rel in paths[:REMOVAL_CHECK_CAP]:
        try:
            rsha = (remote_map.get(rel) or [None, None])[1]
            if not rsha:
                continue
            # M96（A100，P2）：判据**反向**了。
            #
            # 原写法：
            #     rd = api("GET", f"/git/blobs/{rsha}")
            #     if not isinstance(rd, dict) or rd.get("encoding") == "base64":
            #         continue
            #
            # 而 GitHub 的 blob **恒以 base64 返回** —— 本模块自己就有两处
            # 正确的写法（8273 / 8345 行）：`!= "base64"` 才放弃，取到后
            # `base64.b64decode(...)`。这里写成了 `== "base64"` 就放弃，
            # 于是命中条件的是**全部**请求，结果整段丢弃。
            #
            # 实测（mock 与真实 GitHub 同形态）：远端 80 行、本地删到 20 行，
            # 函数返回 (0, [])，提示一次都不出现 —— 保护写了，触发条件不可达。
            #
            # 而 `api(...)` 在检查**之前**就已经发出：成本付了 100%，
            # 收益拿到 0%。实测 60 个文件把推送期 blob 请求从 20 拉到 80。
            #
            # 改用 _remote_bytes()：走 raw 通道，顺带解决 JSON 形式对
            # >1MB 不返回 content 的老问题（见其注释）。
            rraw = _remote_bytes(rsha)
            if not rraw:
                continue
            rlines = set(rraw.decode("utf-8", "replace").splitlines())
            # M98（A100，P3）：原先这里内联了一份 `open()` 读本地文件，
            # 而模块里早有 _local_bytes()（5145 行）。内联版用
            # `os.path.isfile` 跟随链接读**目标内容**，_local_bytes 对软链
            # 返回的是**链接目标路径**（git 的 symlink blob 语义）—— 两者
            # 不一致。这是"同一件事两份实现"的又一个实例（M73 同族）。
            lraw = _local_bytes(rel)
            if lraw is None:
                continue
            llines = set(lraw.decode("utf-8", "replace").splitlines())
            for ln in sorted(rlines - llines):
                removed += 1
                if len(sample) < limit:
                    sample.append((rel, ln[:100]))
        except BaseException:
            continue
    return removed, sample


#
# 背景：387 处 print() 散布全文件，SystemExit 从深层直接抛出。后果是
#   · --json 无法实现 —— 纯数据消费者被迫去剥人类文案
#     （阶段 0 探路时就撞上了：_report_conflict_artifacts() 自己 print，
#      把提示插到 JSON **前面**，json.loads 直接失败，只能先捕获成 warnings）
#   · --quiet 无法实现 —— 分不清「该说的结论」与「进度噪音」
#   · 测试焊死在实现上 —— 断言靠抓 stdout，改一句提示文案就红
#
# 阶段 2 的做法：所有面向用户的输出经过 Ui，两种渲染：
#   text（默认） 逐行 print，与历史输出**逐字一致**
#   json         收集成结构化事件，不再直接 print，最后一次性 dump
#
# 为什么「逐字一致」是硬要求：现有 22 套回归靠抓 stdout 断言，
# 输出一变就红。阶段 2 只加通道、不改任何一行文案 —— 于是测试全绿
# 证明的是「加了这层没改行为」，而不是「改了行为测试还没跟上」。
# 把文案收进 Ui 统一重排版是下一步的事。
#
# 过渡态说明：Ui 目前是模块级单例，与 OWNER/REPO/ROOT 那批全局变量一样，
# 属于阶段 3「Config 显式传递」要收掉的东西。先有边界，再谈注入。


class Ui:
    """面向用户的输出出口。

    mode   "text"（默认，等价于直接 print）/ "json"（收集事件，不落地）
    quiet  text 模式下是否压掉 info 级噪音（结论类照常输出）
    """

    def __init__(self, mode="text", quiet=False):
        self.mode = "json" if mode == "json" else "text"
        self.quiet = bool(quiet)
        self.events = []
        #: 已有别处负责本次 JSON 输出（如 show_status 自己 json.dump）
        self.suppressed = False
        #: 本次运行是否带 --json（用于给不支持的子命令提示）
        self.json_warned = False

    # ---- 出口 ----------------------------------------------------------
    def line(self, text="", kind="info"):
        """一行自由文本。

        kind 决定 JSON 里的分类，也决定 quiet 是否压掉它：
          info    进度噪音（quiet 时静默）
          warn    警告（quiet 时保留 —— 压掉警告等于骗人）
          error   错误（同上）
          ok      成功结论（同上）
        """
        self.events.append({"kind": kind, "text": text})
        if self.mode == "text":
            if not (self.quiet and kind == "info"):
                print(text)
        return self

    def warn(self, text):
        return self.line(text, "warn")

    def error(self, text):
        return self.line(text, "error")

    def ok(self, text):
        """成功级。quiet 下同样保留（见 line 的 quiet 说明）。

        与 M27（`rt()` 生产零调用、但测试依赖 → 判为面向测试的访问器）
        **同一形态**：本方法在生产代码里零调用 —— 脚本的成功输出有
        467 处都走 print、不在 Ui 通道里，而 Ui 通道的 48 处集中在
        「防护拦下与放行提示」，那两类对应 warn / error，没有 ok 的
        语义位置。但 `test_ui.py` 依赖它验证 quiet 语义，
        所以按 M27 的判据**保留并登记用途**，不是死代码。
        """
        return self.line(text, "ok")

    def verdict(self, v):
        """一道 Guard 的结论。

        text 模式下**不再重复打印**：Guard 自己已经 print 了详细的人类
        指引，这里再渲染一遍就是两遍。JSON 模式下则把结构化字段收进
        events —— 这才是它存在的意义。
        """
        # M39（A54）：原先只收 6/9 个字段 —— detail / not_recommended /
        # bypassed / extra 被丢掉。而它们**不是备而未用的空壳**：
        #   · not_recommended —— RewindGuard 实测填了
        #     ["--pull", "--mark-synced"]（L12250）
        #   · bypassed        —— StaleLocalGuard / RewindGuard 填 True
        #     （L12254 / L12386）
        # 丢掉前者，JSON 消费者拿不到「这几个操作别用」；
        # 丢掉后者，它分不清这次是**真拦下**还是**被 --force 放行**。
        # 两者恰好是 --json 通道最该回答的两问。
        self.events.append({
            "kind": "verdict", "guard": v.guard_id, "level": v.level,
            "action": v.action, "reason": v.reason, "remedy": list(v.remedy),
            "detail": v.detail,
            "not_recommended": list(v.not_recommended),
            "bypassed": bool(v.bypassed),
            "extra": dict(v.extra or {}),
        })
        return self

    def flush(self):
        """json 模式下落地。text 模式是逐行即时输出，无需 flush。"""
        # N136 附带：--status 等子命令自己 json.dump 并直接打印，
        # 不走 Ui。此时再 flush 一次会输出**两份** JSON，整体不可解析
        #（实测：json.loads 报 "Extra data"）。
        # 已由别处输出过的，这里让路。
        if self.suppressed:
            return self
        if self.mode == "json":
            json.dump({"events": self.events}, sys.stdout,
                      ensure_ascii=False, indent=2, default=str)
            sys.stdout.write("\n")
            sys.stdout.flush()
        return self

    # ---- 兼容 ----------------------------------------------------------
    # 让 `ui("...")` 与 `ui.line("...")` 等价：过渡期两种写法都会出现，
    # 不支持调用形式会让「少打了 .line」变成 AttributeError 而不是输出。
    __call__ = line


#: 模块级单例。阶段 3 收进 Config 后改为显式传递。
_UI = Ui()


@contextlib.contextmanager
def ui_keep_all():
    """N343（第一百〇七轮，中危）：临时关掉 quiet，让一段输出**全部**保留。

    `--quiet` 的定义是「压掉 info 级**进度噪音**」（见 Ui.line 的 kind 说明）。
    但接上 CLI 之后实测发现它压掉的恰恰不是噪音：

        507 处 print   → 完全不走 Ui 通道，--quiet 对它们零效果
        Ui 通道 49 处  → 其中 42 处在 Guard.check() 里

    `--status` 实测 quiet 开关前后**都是 14 行**（一行没少，全是 print）；
    而 `RewindGuard` 被拦下时输出从 7 行变 1 行 ——
    **保留了结论，压掉了依据和出路**：

        quiet=False  含『解除方式』: True   含 reflog: True
        quiet=True   含『解除方式』: False  含 reflog: False

    用户的处境是：看到「❌ 已中止」，却不知道**是哪个文件**、
    也拿不到 `--force-file` / `reflog` 这条唯一出路。
    而 `RewindGuard` / `StaleLocalGuard` 是 `action="return"`、
    **不抛 GuardBlock**，所以 `__main__` 里那句
    `except GuardBlock: print(str(e))` 不会执行，没有兜底。

    为什么用上下文而不是把 42 处调用逐个改成 warn：
      · 逐个改会漂移 —— 新增一个 Guard 忘了改就又漏
      · Guard.check() 只在**被拦下时**输出，那时**没有一行是进度噪音**：
        全是"为什么被拦下 + 怎么解除"。整段保留才是正确的粒度。
      · try/finally 保证不残留状态（N149/N150 的教训）。
    """
    u = get_ui()
    old = u.quiet
    u.quiet = False
    try:
        yield u
    finally:
        u.quiet = old


def set_ui(mode=None, quiet=None):
    """切换全局 Ui 的渲染模式（--json / --quiet 入口用）。

    保留已收集的事件：调用点可能已经输出过内容，换模式不该把前面的丢掉。
    """
    global _UI
    old = _UI
    _UI = Ui(mode=mode if mode is not None else old.mode,
             quiet=old.quiet if quiet is None else quiet)
    _UI.events = old.events
    return _UI


#: 本次运行登记到的待删除清单（由 report_pending_deletes(emit=False) 写入）。
#:
#: 放在模块级是为了让**运行出口**统一打印：清单必须在最后出现一次，
#: 否则会被几十行推送输出冲到屏幕外（AI / CI 通常只读最后几行）。
#: 但它又不能打两遍 —— 那是"同一份逻辑两处"的老毛病。
_RUN_PENDING_DELETES = None


#: N382（第一百二十二轮，中危）：**执行侧**真正送出过的删除条目。
#:
#: 与 _RUN_PENDING_DELETES（登记侧、推送**前**算出）必须分开：
#: 删除生效后，出口重算待删除清单必然为空，若只看登记侧就会打印
#: 「已不在待删除清单中（删除功能关闭时表示未被处理）」——
#: 而实际上已经删了。同一次输出里上句说「将删除 1 个」、
#: 下句说「未被处理」，用户无从判断到底删没删。
_RUN_DELETED_PATHS = None


#: N383（第一百二十三轮，中危）：本次**已把内容推到远端**的那个 ref。
#:
#: `_main()` 里「失败即清理」的 try 在**本地补提交结束**就合上了，
#: 而之后还有三步在 try 之外：save_state / ensure_pr / do_merge。
#: 此时远端分支上已经有本次全部内容（API：PATCH 已完成；git：push 已完成）。
#:
#: 实测（注入 save_state 抛 OSError，模拟磁盘不可写）：
#:   · audit(push_fail) 一次都没触发 —— 而它的 docstring 写的存在理由
#:     正是「远端可能已经建了分支/blob，用户看到的只是一堆报错，
#:     事后要能回答『那次到底推到哪一步了』」
#:   · _cleanup_failed_branch 在 try 里，够不到
#:   · OSError 不是 SystemExit，顶层只接 GuardBlock / KeyboardInterrupt /
#:     EOFError → 用户看到 traceback
#: 同一个"失败"语义，跨过 try 的边界就完全变了样。
#:
#: 记在这里是为了让 _main_wrapped 的兜底 except 能说出"推到哪个 ref 了"。

#: N384（第一百二十三轮，低危）：git 通道下 `branch_created_now` 恒 None，
#: 因为只有 API 通道那一支给它赋值（git 通道由 `git push` 自己建分支，
#: 预建反而多一次 API 往返）。而它 4 处使用全在失败通道上，于是
#: audit 的 branch / cleaned 在 git 通道下恒为 None / False ——
#: 即便分支真的被建出来并带上了内容。
#: 事后追溯（"那次到底推到哪"）拿不到分支名。改用本变量即可消除：
#: 它记的是"内容实际落在哪个 ref 上"，与通道无关。
_LAST_TARGET_REF = None


#: N383：`_main()` 内的 except BaseException 是否已经记过 push_fail。
#: 兜底那一层据此去重 —— 否则每个异常会记两条一模一样的流水。
_PUSH_FAIL_AUDITED = False


#: M44（A54）：本次运行里被「非 UTF-8」挡在 local_seen 之外的路径数。
#:
#: 由 sync_local_seen() 累加。它**返回**这个数（N7 就要求了），但 7 个
#: 调用点里 6 个丢弃返回值，而 report_removals 的 skipped_non_utf8
#: 参数从未被传入 —— 于是「N 个非 UTF-8 路径已排除在删除检测外」
#: 这句提示**永不打印**（参数恒为 0）。
#:
#: 这句提示的后果是实的：这些路径永久进不了 local_seen，日后删除它们
#: 会被判成「远端独有」而静默跳过（判据 3）。静默丢弃正是删除功能
#: 最该说清楚的事。
#:
#: 存**集合**而不是计数：sync_local_seen 一次运行会被调 6~7 次，而
#: 同一条非 UTF-8 路径每次都还在 lmap 里（它进不了 local_seen），
#: 于是每次都会被算一遍 —— 计数会虚高成 6 倍。按路径去重才是真的
#: 「有几个路径受影响」。
_NON_UTF8_DROPPED = set()


#: 本次运行是否**已由别处**输出过 JSON（N136/N149）。
#:
#: 放在模块级而不是挂在 Ui 实例上：set_ui() 会**重建实例**，
#: 挂实例上的标志会被悄悄丢掉 —— 实测置位后读到仍是 False，
#: 于是又输出一份，整体不可解析。
_JSON_EMITTED = False
#: 纯数据流模式（--mine）。与 --json 同属一类输出契约，但 --mine 不走
#: Ui 通道，故不能用 Ui.mode 表示 —— 见 _stream_active() 的说明。
#: 必须进 reset_per_run_globals（判据 5：per-run 全局不复位会跨运行残留）。
_STREAM_MODE = False


#: M80：本次运行**是否走到了分派区**（各子命令真正开始干活的那一段）。
#: finally 的「本子命令暂不支持 --json」要看它 —— 缺 token / 仓库校验 /
#: 无基线 / 取不到 ref 都在分派**之前** return，全部落进那一支，
#: 于是它说"结构化输出目前只在 --status 上可用"，而当前命令**就是**
#: --status（自相矛盾）。
_DISPATCH_REACHED = False

#: M80-2（A84）：本次运行**是否已经打过**「本子命令不支持 --json」。
#: 用于让 `--prune`（它自己 print 一句）与 finally 那一句**互斥** ——
#: 见 `_warn_json_unsupported()` 的说明。per-run，必须进复位清单。
_JSON_UNSUPPORTED_WARNED = False

#: N416（高危，第一百三十四轮）：`_auth_hint` 的重入保护标志。
#:
#: `api()` 失败 → `_raise_api_error()` → `_auth_hint(code)` → `api("/user")`
#: —— 而 /user 用的**还是同一个 `api()`**，它失败时又会走同一条链路。
#: 没有重入保护就是无限递归。
#:
#: 实测（注入 `_curl_once` 让它恒返 403，统计真实请求次数）：
#:     500: 3   502: 3   503: 3   504: 3   429: 3
#:     403: **329**                    ★
#:     422: 1   404: 1   401: 1   200: 1
#: 其中 `/user` **单独被打了 328 次**。
#:
#: 它没把栈撑爆是因为 `_auth_hint` 内部 `except BaseException: valid = None`
#: 把 `RecursionError` 也吞了 —— 递归到解释器上限后逐层返回，最外层照常抛
#: SystemExit，**从外面看只是一次普通的 403 报错**。
#:
#: 危害在限流时放大：一次 403（限流的常见形态）会额外产生 300+ 次请求，
#: 把「额度已尽」推向「彻底打不出去」，且耗时从 0.2 秒变成几分钟。
#:
#: 本条**第 4 次**被并发推送撤销（#247 / #251 各一次），已进判据 18 的
#: ANCHOR_BASELINE —— 再消失时 check_invariants.py 会硬失败。
_AUTH_PROBING = False
# M104（A104，dogfood 实测）：--dry-run 用 `raise _Cancel()` 表达
# "预览结束、不做推送"，于是它走了**取消**这条通道：
#     _LAST_RUN_CANCELLED = True → 顶层 sys.exit(4)
#
# 而 4 的契约是「取消：这次没跑成，需要重跑（非交互场景多半要加 --yes）」。
# --dry-run 是**故意**不跑的，用户已经显式声明了；CI 用它做预检步骤
# 会拿到 4，按契约理解成"失败，重跑并加 --yes" —— 而实测加 --yes
# 之后退出码**仍然是 4**（--dry-run 与 --yes 无关）。
#
# 更矛盾的是输出：全程零「已取消」字样，明写「[dry-run] 未做任何推送」。
# 用户看到的"成功预览"与退出码说的"没跑成"正好相反 ——
# 与脚本自己在退出码契约注释里点名要消灭的形态同族
# （"CI 不读提示，只看退出码"）。
#
# 按 M59 的教训：这是 per-run 状态，必须进复位清单，否则长驻调用里
# 上一次的 --dry-run 会让后续真实推送的取消被当成 dry 而不置位。
_DRY_RUN_ONLY = False


def _mark_dispatch_reached():
    global _DISPATCH_REACHED
    _DISPATCH_REACHED = True


def _warn_json_unsupported(msg=None):
    """M80-2（A84，2.4 那条未验证观察）：「本子命令不支持 --json」的
    **唯一**告知口。

    A84 静态推测 `--prune --json` 会打**两句**措辞略不同的提示：
    prune 自己一句 + finally 那一句。实测**只打一句**，但原因是侥幸 ——
    prune 分支漏了 `_mark_dispatch_reached()`（M82 补 audit /
    clean_conflicts 时以「它自己 print 了一句，所以一直有提示」为由
    跳过了它）。于是：

        只补置位、不动 finally  →  两句（A84 的推测成真）
        置位 + 本帮手           →  一句（且 reached 语义变准）

    换句话说：那句提示不重复**靠的是漏置位**，不是靠设计。任何让
    `reached` 变准的改动都会把它变成重复 —— 这正是"用当前恰好没有
    可见后果来正当化一个不一致"的典型。

    三个动作必须成对，缺一不可：
      1. 置位 _DISPATCH_REACHED（本分支确实进了分派区）
      2. 打印一次
      3. 置位 _JSON_UNSUPPORTED_WARNED（让 finally 不再打第二句）
    """
    global _JSON_UNSUPPORTED_WARNED
    _mark_dispatch_reached()
    print(msg or ("  ! 本子命令暂不支持 --json，按文本输出"
                  "（结构化输出目前只在 --status / --scope 上可用）"))
    _JSON_UNSUPPORTED_WARNED = True


def _cancelled(msg="已取消"):
    """打印取消提示并置位取消标志 —— 退出码 4 的**唯一**置位口。

    N365（第一百一十四轮，中危）：N362 把取消的退出码从 0 改成 4，
    **但只改了推送路径那一处** —— `_LAST_RUN_CANCELLED` 全脚本仅在
    `except _Cancel` 置位一次。其余 5 处「已取消」都是
    `print("已取消")` 之后直接 `return`：

        6359 --merge            确认合并时答否
        6613 --close-pr         确认关闭时答否
        6657 --delete-branch    非 task/* 分支的二次确认答否
        6670 --delete-branch    确认删除时答否
        10101 --reset-baseline  确认重设时答否

    实证（非交互环境，模拟 CI）：

        --reset-baseline 拒绝 → main() 正常返回
        ★ _LAST_RUN_CANCELLED = False → 顶层不 exit → **退出码 0**
        对照推送路径（同一环境）→ True → exit 4

    **同一个脚本、同一种"用户没确认"的事实，给出 0 和 4 两个退出码。**

    退出码 4 的注释把理由写得很清楚，且对这 5 处**逐字成立**：

        4 = 取消：这次没跑成，需要重跑（非交互场景多半要加 --yes）。
        不能是 0（CI 会当成功），也不能是 1（那是"出错"，
        而取消是正常流程）。

    尤其 --reset-baseline：CI 拿到 0 会认为基线已重建，而实际上旧基线
    原样留在那里，后续所有"远端是否被他人改动"的判断都在用一个用户
    以为已经换掉的参照。

    抽成函数而不是各置一次：逐个补必然再漏（新增确认点时想不起来），
    与 N306 的 `pending_deletes_union` 同一考量。
    """
    global _LAST_RUN_CANCELLED
    print(msg)
    _LAST_RUN_CANCELLED = True


def _mark_json_emitted():
    global _JSON_EMITTED
    _JSON_EMITTED = True


def _stream_active():
    """当前是否处于「纯数据流」模式：`--json` 或 `--mine`。

    两者是**同一类**输出契约，只是消费者不同：

      · --json  → 消费者是 json.loads / CI，混一行文案就解析失败
      · --mine  → 消费者是 `| while read f`，混一行文案就被当成文件名

    分一个函数而不是各处写 `opts.get("json")`：
    `_exec_bit_reliable()` 这类路径拿不到 opts（它在分派之前就可能
    被 local_state_map 触发），只能读全局。

    --mine 不经过 Ui，故 Ui.mode 反映不出它 —— 单独用 _STREAM_MODE 记。
    """
    if _STREAM_MODE:
        return True
    return _json_active()


def _json_active():
    """当前是否处于 `--json` 结构化输出模式（M86 / M81）。

    为什么要一个函数而不是各处直接 `opts.get("json")`：
    `init_baseline()` 这类早期路径**拿不到 opts**（它在分派之前就被调用，
    且签名里没有 opts）。Ui 实例是全局的、且在 `load_state` **之前**
    就已 set（`set_ui` 在 L10071，`load_state` 在 L10186），
    所以读 `get_ui().mode` 是这两条早退路径唯一可行的判据。
    """
    try:
        _u = get_ui()
        return bool(_u is not None and getattr(_u, "mode", None) == "json")
    except Exception:
        return False


def get_ui():
    return _UI

# ==================== Guard 管线（阶段 1：把顺序变成声明）====================
#
# 背景：防护已从 4 层长到 5 层，而「谁必须在谁之前」**只写在注释里**。
# 例如「未解决冲突拦截必须在 P0-1 之前」—— 那条注释甚至还记着试错过程：
# 放到待推集合最终确定之后，结果完全执行不到（pull 产生冲突后
# synced_commit 不刷新，推送会先被 P0-1 拦下）。
#
# 注释约束不了未来的改动：新增第 6 层、或调换两段顺序时，没有任何东西
# 会阻止你。而这类错误的表现是「防护静默失效」—— 正是本工具最想消灭的
# 那一类。
#
# 阶段 1 的做法：判据搬进 Guard，顺序写进 GUARDS 列表 + 每个 Guard 的
# after 声明，由 validate_guard_order() 强制。调换顺序若违反声明，
# 校验直接报错，而不是等人读注释。
#
# 阶段 1 **刻意不动 print**：Guard 内部照原样打印，输出逐字不变，
# 于是现有全部测试能直接验收「拆分本身是对的」。把输出收进 Verdict、
# 再让 Ui 统一渲染是阶段 2 的事 —— 两件一起做的话，测试一红就分不清
# 是拆错了还是渲染改错了。
#
# 阶段 1 也**不统一退出方式**：有的 Guard 是「打印完 return」、
# 有的是 raise _Cancel（要走分支清理）、有的是 raise SystemExit。
# 差异由 Verdict.action 显式承载，等阶段 2 引入 Ui 时一并收敛。

PASS, BLOCK, WARN = "pass", "block", "warn"


class Verdict:
    """一道防护的判定结果。

    level    PASS / BLOCK / WARN
    guard_id 哪道防护（用于 bypass 判定与日志）
    action   BLOCK 时怎么退出："return" 交给调用方 / "cancel" raise _Cancel
             （要走分支清理通道）/ "abort" raise SystemExit（硬失败）
    """

    __slots__ = ("level", "guard_id", "action", "reason", "remedy",
                 "detail", "not_recommended", "bypassed", "extra")

    def __init__(self, level, guard_id, action="return", reason="", remedy=(),
                 detail="", not_recommended=(), bypassed=False, **extra):
        self.level = level
        self.guard_id = guard_id
        self.action = action
        self.reason = reason
        self.remedy = list(remedy)
        # detail：完整的人类文案（SystemExit / 交互提示用）。
        # reason 是一句话摘要，detail 是可直接显示的长文 —— 两者分工不同，
        # 都塞进 reason 会让 JSON 消费者拿到一堆带 emoji 和换行的字符串。
        self.detail = detail
        # not_recommended：明确**不该**建议的操作。
        # 回退拦截里 --pull / --mark-synced 会被同一判据拦下，把它们列进
        # remedy 等于把用户推进死循环，必须能显式表达「这几个别用」。
        self.not_recommended = list(not_recommended)
        self.bypassed = bool(bypassed)
        self.extra = extra

    def __repr__(self):
        return f"<Verdict {self.guard_id}: {self.level}>"

    def __bool__(self):
        return self.level != BLOCK


class PushCtx:
    """Guard 之间共享的上下文。

    用普通属性而不是 dict：写 ctx.state 拼错了是 AttributeError（立刻炸），
    写 ctx["state"] 拼错了只是多一个键（静默返回 None）。
    """

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def bypass(self):
        """全局 --force-overwrite 是否生效。

        只有**全局**开关能放行 P0-1 这类全局闸门；--force-file 是细粒度的，
        只能放行文件级判定（见 StaleLocalGuard 的说明）。
        """
        return bool(getattr(self, "force", False))

    def get(self, name, default=None):
        return getattr(self, name, default)


class Guard:
    """一道防护。

    id          唯一标识（bypass 判定、日志、顺序声明都用它）
    after       必须在这些 id 的 Guard **之后**运行
    bypassable  --force-overwrite 能否放行这道防护
    """

    id = ""
    after = ()
    bypassable = True

    def check(self, ctx):
        raise NotImplementedError


def _emit_verdict(v):
    """把一道 Guard 的结论收进 Ui（--json 的 events）。

    永不抛异常：它只是记录手段，不能成为新的故障点。
    """
    try:
        _ui = get_ui()
        if _ui is not None and hasattr(_ui, "verdict"):
            _ui.verdict(v)
    except BaseException:
        pass


def _enforce_audited(v, tag):
    """_enforce 之外，把阻断/放行也记进审计流水（N251）。

    五道 Guard 分两条路跑：run_guards() 只跑前三道（PRE_PUSH_GUARDS），
    而 FileBaselineGuard / SizeGuard 在推送流程里单独调 _enforce()。
    审计只写在 run_guards 里，于是后两道的**阻断与放行都不留痕** ——
    `guard_pass` 声称"过了所有 Guard"，实际只覆盖 3/5。

    事后复盘"这次到底被谁拦下"时，最常问的两道（文件级基线、大文件）
    恰恰查不到。
    """
    try:
        if v.level == BLOCK:
            # M168（A153，中危）：补 reason / detail。
            #
            # 本函数的 docstring 写的是「记进审计流水……是事后复盘
            # 「为什么这次没推上去」的**唯一凭据**。stdout 一滚就没」。
            # 而实测（A153）真推送被 SizeGuard 拦下时，审计原文是：
            #   {"kind":"guard_block","guard":"size_guard","level":"block","passed":[]}
            # —— 只有 guard / level / passed 三个字段，**没有文件名、
            # 没有字节数、没有 reason**。
            #
            # 于是「唯一凭据」里没有任何关于「为什么」的信息：事后只能
            # 知道「被 oversize_blob 拦了」，不知道是哪个文件、多大。
            # 而这两项 N487 刚刚补进了 reason 与 detail —— 只是没进审计。
            #
            # 这正是 A153 审的那份报告（192 轮）主题的未完成部分：
            # N487 覆盖的是 JSON 通道，审计通道一个字段都没跟上，而它
            # 恰恰被自己的注释立为「唯一凭据」。
            audit({"kind": "guard_block", "guard": tag,
                   "level": v.level, "passed": [],
                   "reason": getattr(v, "reason", ""),
                   "detail": getattr(v, "detail", "")})
        elif v.level == WARN:
            # N488（A117）：原先只分 BLOCK / 非 BLOCK，于是 WARN 被记成
            # `guard_pass` —— 读审计的人会以为这道 Guard 放行了，实际是
            # "警告且放行了"，两者在复盘时含义完全不同。
            #
            # ⚠️ 诚实说明：这条错**当前不可观测** —— `audit()` 有
            # `if _dry_this_run(): return`（--dry-run 不写审计，因为预演
            # 什么都没做），而 SizeGuard 是唯一返回 WARN 的 Guard 且
            # **只在 ctx.dry 时返回**。所以 dry 下根本没有记录可读。
            # 哪天 WARN 出现在非 dry 场景、或 dry 允许写审计，它就活了。
            audit({"kind": "guard_warn", "guard": tag,
                   "level": v.level, "passed": [{"guard": tag}],
                   # M168（A153）：同上。N589 已让 dry 下的 Verdict 也
                   # 带上文件名与字节数，这里一并进审计 —— 哪天 WARN
                   # 出现在非 dry 场景（或 dry 允许写审计）就直接可用。
                   "reason": getattr(v, "reason", ""),
                   "detail": getattr(v, "detail", "")})
        else:
            audit({"kind": "guard_pass", "passed": [{"guard": tag}]})
    except BaseException:
        pass          # 审计绝不阻断主流程
    _enforce(v)


def _enforce(v):
    """按 Verdict.action 执行退出。非 BLOCK 时什么都不做。"""
    # M18（A16 / A24 实证）：`Ui.verdict` 零调用点 ——
    # 它存在的唯一意义是把 Guard 结论收进 JSON events，
    # 而没有任何地方调它，于是 --json 拿不到任何 Guard 结论。
    #
    # 这是判据 3 那一类（保护写了、触发点不可达）的第 5 个实例。
    # 接线放在这里而不是逐个 Guard 里：五道 Guard 分两条路跑
    #（run_guards 前三道 + 单独调 _enforce 后两道），
    # 而**两条路最终都经过 _enforce**，这里接一次就全覆盖。
    _emit_verdict(v)
    if v.level != BLOCK:
        return
    # N74：四个会 BLOCK 的 Guard 没有一个置位 _BLOCKED_BY_GUARD，
    # 而它是顶层决定退出码的唯一依据。于是被防护拦下时退出码是 0，
    # CI 会当成推送成功 —— 与脚本自己声明的「被拦下 = 2」约定不符，
    # 而且这是最糟糕的一种错：改动没丢，但流水线以为成功了。
    global _BLOCKED_BY_GUARD
    if _BLOCKED_BY_GUARD is None and getattr(v, "guard_id", None):
        _BLOCKED_BY_GUARD = v.guard_id
    if v.action == "abort":
        # 优先用 detail：它是完整文案；reason 只是一句话摘要。
        #
        # N111：N74 只修了 return 型那半 —— 它保证 `_BLOCKED_BY_GUARD`
        # 被置位，但 abort 型走的是 `raise SystemExit(str)`，
        # 顶层 `except SystemExit` 拿到的 code 仍是 1（或 None）。
        # 于是被防护 abort 时退出码还是 1，CI 依旧分不清
        # 「被拦下」与「真出错」。退出码必须挂在异常上带出去。
        # GuardBlock 是 SystemExit 子类且 code 固定为 2，
        # 不能像 SystemExit(msg, code) 那样传第二个位置参数
        #（那会把它当成构造参数而非退出码）。
        # N241：冲突未解决用 3，其余被拦下用 2。
        # 两者的处置方式不同（前者必须人工合并，后者可自动解除）。
        _code = 3 if v.guard_id == "unresolved_conflict" else 2
        raise GuardBlock(v.detail or v.reason, code=_code)
    if v.action == "cancel":
        raise _Cancel()
    # "return"：调用方自行 return（_main 的收尾路径）


class UnresolvedConflictGuard(Guard):
    """未解决冲突拦截：带冲突标记推送 = 把损坏文件推上去。

    必须在 StaleLocalGuard（P0-1）**之前** —— 试过放在待推集合最终确定
    之后（那时集合最准），结果完全执行不到：pull 产生冲突后 synced_commit
    不刷新，推送会先被 P0-1 拦下。所以这里用**预计算**的候选集合。

    且它**不适用** --force-overwrite：那是覆盖远端，解决不了本地冲突。
    """

    id = "unresolved_conflict"
    after = ()
    bypassable = False

    def check(self, ctx):
        state = ctx.state
        if not state.get("conflicts"):
            return Verdict(PASS, self.id)
        lmap_early = local_state_map()
        explicit = ctx.get("explicit")
        pending = set(explicit) if explicit else set(
            detect_changes()) | set(baseline_changed_files(state, lmap_early))
        pending = {safe_rel(p) for p in pending} - {None}
        # N375（第一百一十九轮，中危）：两侧口径必须一致 —— 一侧归一化了、
        # 一侧没有，于是 `conflicts = ['./a.txt']` 时这道 Guard **完全失效**
        #（决定性对照：'./a.txt' 放行、'a.txt' 拦下）。
        # 它不是计数失真而是**绕过防护**：带冲突标记的文件会被推上去。
        # 「字符串相等 vs 路径相等」家族（N128 / N368 / N220）里第一个
        # 绕过防护的。修在 Guard 这一侧而不是 sanitize 那一侧：状态文件
        # 可能来自手工编辑、旧版本遗留、跨版本升级，入口那一处拦不住。
        _conf_norm = {safe_rel(r) for r in state["conflicts"]} - {None}
        unresolved = sorted(r for r in _conf_norm if r in pending)
        if not unresolved:
            return Verdict(PASS, self.id)
        # N374（第一百一十九轮，中危）③：文件**已删除**时，上面那句
        # 「手工改好后 --resolve <文件>」是**走不通的** ——
        # resolve() 对本地不存在的文件必然 SystemExit（需要 --yes）。
        #
        # A93 记录的正是这个体验：用户照提示试了 --clean-conflicts /
        # --resolve / --mark-synced / --reset-baseline 四条命令，
        # **没有一条能清**这条记录。现在 --resolve --yes 已经是出口，
        # 但**拦下用户的第一句话**仍指向不通的那条 —— 用户会再一次
        # 得出「没有出路」的结论。
        #
        # 所以这里按「本地还在不在」分给出路。判据用 _local_exists：
        # 它只在确实没有时返回 False，"查不到"（EACCES 等）按「还在」
        # 处理 —— 与删除判据同源，不会把"读不到"当成"已删除"。
        _gone = [r for r in unresolved if not _local_exists(r)]
        # A4-H3 同族：集合提到推导式外一次建好，不在逐元素判定时重建。
        _gone_s = set(_gone)
        _here = [r for r in unresolved if r not in _gone_s]
        _lines = []
        if _here:
            _lines.append(
                f"  手工改好后逐个运行：python3 push_api.py --resolve <文件>\n"
                f"  本地已删掉的（放弃改动、保留远端那份）要加 --yes：\n"
                f"    python3 push_api.py --resolve <文件> --yes"
                f"\n    待改：{_here[:10]}")
        if _gone:
            _lines.append(
                f"  本地已删除（放弃改动、保留远端那份）的文件，"
                f"--resolve 需要 --yes：\n"
                f"    python3 push_api.py --resolve {_gone[0]} --yes"
                + (f"\n    同类共 {len(_gone)} 个：{_gone[:10]}"
                   if len(_gone) > 1 else "")
                # N553（第一百八十一轮，低危）：原句写的是「想保留本地那份，
                # 改用 --pull」，而本分支的前提恰恰是**本地那份已经不存在**
                # —— --pull 拉回来的是**远端**那份（实测内容 v2，不是用户
                # 删掉的 v1）。用户若理解为「能找回我删掉的那份」，就会在
                # 一个错误的预期下放弃比对。与 resolve() 自己那句
                # 「用 --pull 把它拉回来」对齐：只说拉回一份，不承诺内容。
                + f"\n    想拿回一份内容，改用：python3 push_api.py --pull"
                + f"（拉回的是**远端**那份，不是你删掉的版本）"
                # M155（A143，中危）：上面这条出路在**本分支最常见的场景里
                # 恒无效**。`pull()` 的判据是「远端相对基线有没有变」；而
                # 本分支的前提是**本地删了**、远端没动 —— A143 的决定性对照：
                #   场景 A（远端没再变）--pull → 「没有需要合并的远端改动」，
                #            rc=0，文件不回、conflicts 不清 → 空转
                #   场景 B（远端此后又变过）--pull → 才真的补齐并清记录
                # 也就是说：把它写进出路清单，等于给了一条看起来是出路、
                # 实测多半空转的命令 —— 与 N519/N374 同族（"给了但做不到"）。
                # 如实写出边界，让用户一跑就明白为什么没反应。
                + f"\n      ⚠ 但远端此后没再变过时，--pull 会输出"
                  f"「没有需要合并的远端改动」而什么都不做"
                  f"（不会拉回文件、也不会清这条记录）")
        msg = (
            f"本次要推送的文件中有 {len(unresolved)} 个冲突尚未解决："
            f"{unresolved[:10]}\n"
            + "\n".join(_lines) + "\n"
            f"  或只推送其它文件（显式点名即可绕开这些冲突文件）。\n"
            f"  （冲突文件不适用 --force-overwrite："
            f"那是覆盖远端，解决不了本地冲突）"
        )
        return Verdict(
            BLOCK, self.id, action="abort",
            reason=f"{len(unresolved)} 个待推送文件仍有未解决的冲突",
            remedy=([f"--resolve {r}" for r in _here[:10]]
                    + [f"--resolve {r} --yes" for r in _gone[:10]]),
            detail=msg)


class RewindGuard(Guard):
    """本地 git HEAD 是否被回退过（git reset / checkout <old> / stash pop）。

    synced == head_sha 只证明「上次同步时本地包含了远端全部改动」，
    证明不了**现在**还是那样。实测：
        push v3 → git reset --hard HEAD~1（本地回到 v2）→ push
      → P0-1 放行（v3==v3）、第一层放行（远端 v3 == 基线 v3）
      → 本地 v2 覆盖主干，v3 静默消失，全程零报错。

    必须在 StaleLocalGuard 之前：两者正交（一个拦「远端前进」、
    一个拦「本地后退」），但回退是更根本的问题，先报它。
    """

    id = "rewound_local_head"
    after = ("unresolved_conflict",)

    def check(self, ctx):
        rewound, why = _local_head_rewound(ctx.state)
        if not rewound:
            return Verdict(PASS, self.id)
        if not ctx.bypass:
            _UI.error(f"\n❌ {why}")
            _UI.line("   本地文件可能被 git 操作退回了旧版本（reset / checkout / stash pop），")
            _UI.line("   而基线仍记录着「本地已含远端最新」—— 此时推送会把主干静默回退。")
            # 解除方式**不能**再列 --pull / --mark-synced。
            #
            # 那两条现在都会被 _refuse_if_rewound 拦下（这正是要的效果 ——
            # 它们会写 synced_commit/local_head，把回退证据抹掉），于是用户
            # 按提示操作就会撞回同一条判据：
            #     push → 这里拦下 → 建议 --pull
            #     --pull → _refuse_if_rewound 拦下 → return False，不刷新
            #     push → 完全相同的分支 → 再建议 --pull → 无限循环
            # 严重程度确实降了（数据丢失 → 用户卡住），但脚本自己的提示把人
            # 推进了循环，比不给提示更糟 —— 用户会以为是自己操作错了。
            #
            # 真正能解的是**修本地**（reflog + reset），而它只在
            # _refuse_if_rewound 里打印过，这里必须同样给出。
            _UI.line("\n   解除方式：")
            # M135（A123，中危）：切分支时首条**不能**是 `git reset --hard` ——
            # 它重置的是当前分支（实测：main 指针被改写到另一个分支的提交上，
            # 原提交只在 reflog 里可达）。切分支的正确动作是切回去。
            _others = _old_head_on_other_branch(ctx.state.get("local_head") or "")
            _cur_b = _current_branch_name()
            _switched = bool(_others and any(b != _cur_b for b in _others))
            # N568：与 `_refuse_if_rewound` 同源（两处出口必须给出**同一个**
            # 首选动作，否则用户按其中一条执行就撞上 A123 论证过的事故）。
            for _ln in _rewind_remedy_lines(ctx.state):
                _UI.line(_ln)
            _UI.line("     --force-overwrite            # 确知后果时全局放行")
            _UI.line("   （--pull / --mark-synced 在此场景下无效：远端与基线一致时它们")
            _UI.line("     并没有可合并的内容，只会再次被同一条判据拦下。）")
            # N494：--init-baseline 实测无效，列进 remedy 等于给人一条跑完
            # 照样被拦的命令 —— 与上面把 --pull 排除是同一个道理。
            _remedy = (["git checkout %s（切回记录里的分支）" %
                        [b for b in _others if b != _cur_b][0]]
                       if _switched else
                       ["git reflog 找到回退前的提交 → git reset --hard <它>"
                        "（⚠️ 会丢弃未提交改动）"])
            _remedy.append("--reset-baseline（确认本地内容含远端全部改动时）")
            _remedy.append("--force-overwrite（确知后果时）")
            return Verdict(
                BLOCK, self.id, action="return",
                reason=why,
                remedy=_remedy,
                not_recommended=[
                    # N494：实测无效（基线已存在 → 不动 local_head → 仍被拦）
                    "--init-baseline（本场景下实测无效：基线已存在时它不改 "
                    "local_head，跑完仍会被同一条判据拦下）",
                    # 明确排除：这两条会被同一判据拦下，列进 remedy 等于
                    # 把用户推进 push → --pull → push 的死循环
                    "--pull", "--mark-synced"])
        _UI.warn(f"\n  ! {why}，--force-overwrite 已放行，存在回退远端更新的风险")
        return Verdict(BLOCK, self.id, action="return", reason=why,
                       remedy=["--force-overwrite 已生效"],
                       bypassed=True)


class StaleLocalGuard(Guard):
    """第零层 P0-1：本地副本是否过期。

    前面几层只能证明「远端没被别人改」，证明不了「本地是从最新版改的」。
    本地副本一旦过期（CDN 缓存的 tarball、离线包、未 pull 的旧 clone），
    过期的全文 + 你的改动会被整文件覆盖上去，**把远端更新的内容回退掉且不报错**。

    synced 缺失（None）与落后同等危险：那意味着从没证明过本地基于哪个版本
    （init 时检出本地≠远端就会留空），必须拦。原来写成 `if synced and ...`
    会让空值悄悄跳过这层防护。

    只由**全局** --force-overwrite 放行，--force-file 不行：
    P0-1 是全局闸门而非按文件判定，它回答的是「你的本地副本是不是从最新
    主干改的」。用户点名 e1.txt 时并不知道自己的副本已过期，也就无从同意
    「把 e2 的他人改动回退掉」—— 这个同意是无效的。
    """

    id = "stale_local"
    after = ("rewound_local_head",)

    def check(self, ctx):
        state = ctx.state
        head_sha = ctx.head_sha
        # N407（中危）：纯新增文件不该被这道**全局**闸门拦下。
        #
        # "全局闸门不能按文件放行" 的论证（第 129 轮等反复论证过）针对的是
        # **远端已存在的同名文件**：用户点名 e1 时并不知道自己的副本已过期，
        # 于是 e2 的他人改动会被静默回退 —— 那个"同意"是无效的。
        #
        # 但对 **LOCAL_ONLY**（远端树里根本没有它）不成立：推送一个远端
        # 不存在的文件，逻辑上不可能回退任何远端内容，风险为零。
        # 此时拦下的唯一后果是把用户推向 --force-overwrite ——
        # 那是**全局**放行，覆盖面大得多，正是脚本自己劝阻的那个选项。
        #
        # 这里只做"暂缓放行"登记，真正的安全判据在 todo 最终确定之后复核：
        # 此刻 todo 还是空的（files 要等点名/删除合并完才最终确定），
        # 只凭 explicit 放行会被 N370 那类「点名范围外被追加回来」绕过。
        _rstate0 = getattr(ctx, "rstate", None) or {}
        _rels0 = [safe_rel(p) for p in (ctx.explicit or ())]
        if (_rels0 and _rstate0
                and all(r is not None and r not in _rstate0 for r in _rels0)):
            ctx.p0_new_only = True
            _UI.line(f"   · 本次点名的 {len(_rels0)} 个文件远端都不存在（纯新增）——")
            _UI.line("     不存在「回退远端更新」的风险，本层暂不拦下；")
            _UI.line("     最终推送前会复核实际文件集合（混入已存在的文件仍会拦下）")
            return Verdict(PASS, self.id)

        synced = state.get("synced_commit")
        stale_local = (synced is None) or (synced != head_sha)
        # 存回 ctx：后续 --pull / 预览等路径可能要读
        ctx.synced = synced
        ctx.stale_local = stale_local
        if not stale_local:
            return Verdict(PASS, self.id)
        if not ctx.bypass:
            if synced is None:
                _UI.error("\n❌ 基线未记录本地所基于的远端版本（通常因初始化时检出本地 ≠ 远端）。")
                _UI.line("   此时推送会把远端更新整文件回退掉且不报错。")
            else:
                _UI.error(f"\n❌ 本地副本落后于远端：基线上记录本地基于 {synced[:8]}，远端现在是 {head_sha[:8]}。")
                _UI.line("   此时推送会把远端这段时间的更新整文件回退掉，且不会报错。")
            if ctx.wf == "pr":
                # PR 工作流下同样必须拦：任务分支是从**最新主干**上建的，
                # 而你的内容还是旧的 —— 相对分支基点，这等于「把主干改动改回去」，
                # GitHub 合并时会当成你故意回退，直接采纳，依旧是静默覆盖。
                # 只有双方都从同一个旧基点出发、各自改动，才是真并发，
                # 那时 GitHub 才做三方合并并明确报冲突。
                _UI.line("   任务分支基于最新主干，你的内容却是旧的：合并时会被判成「你故意回退」。")
            _UI.line("\n   解除方式（按推荐顺序）：")
            if synced is None:
                # N404（高危，第一百三十轮实测撞出）：
                # synced 为 None 意味着**没有可信基点**，而 --pull 的三方合并
                # 恰恰需要一个基点。此时它会退化成用本地 git HEAD 当基点，
                # 于是「两侧都被当成新增」—— 报出来的冲突**未必是真冲突**，
                # 文件被塞满冲突标记（init_baseline 那 20 行警告写的就是它）。
                #
                # 实测后果比"未必是真冲突"更严重：受害者可以是**脚本自己的
                # 源码**，push_api.py 被写成 SyntaxError 后所有命令（含
                # --resolve / --clean-conflicts 这些善后命令）全部不可用，
                # 连清标记都要手工做。而且 --pull 跑完 synced 仍是 None，
                # 第一层照旧拦着 —— 建议通向 nowhere。
                #
                # 所以「首选」不能无条件给 --pull：没有基点时它既有害又无效。
                _UI.line("     python3 push_api.py --mark-synced # 确认本地已含远端全部改动时，直接声明同步（首选）")
                _UI.line("       · 适用：你确认本地内容与远端一致，只是脚本记录的版本号旧了")
                _UI.line("       · 例：远端删了文件、或改动后内容又等价")
                _UI.line("     python3 push_api.py --pull        # 仅在**确认本地落后**时用")
                _UI.line("       · ⚠ 本状态无可信基点，--pull 会退回用本地 HEAD 当合并基点：")
                _UI.line("         两侧都被当新增 → 冲突**未必是真冲突**，文件被塞满冲突标记；")
                _UI.line("         且跑完 synced 仍为空，本层不会解除（实测）")
            else:
                _UI.line("     python3 push_api.py --pull        # 三方合并主干改动到本地（首选）")
                _UI.line("     python3 push_api.py --mark-synced # 确认本地已含远端全部改动时，直接声明同步")
                _UI.line("       · 适用：你确认本地内容与远端一致，只是脚本记录的版本号旧了")
                _UI.line("       · 例：远端删了文件、或改动后内容又等价，--pull 会说「没有需要合并的」")
            if synced is None and (state.get("files") or {}):
                # N405（中危）：唯一真正能解套的 `--reset-baseline` 不在
                # 被卡住的那个提示里。
                #
                # 实测：基线处于「存在但 synced_commit=None」的残缺状态，
                # 用户最自然的反应是再 init 一次，而 init 只回一句
                # "基线已存在…请加 --reset-baseline" —— 能解套的命令写在
                # **另一条命令**的输出里，用户此刻看到的却是第一层的三选一，
                # 其中 --pull 实测无效（N404）、--mark-synced 的前提
                # "确认本地已含远端全部改动" 在本场景下不成立（本地缺 N 个
                # 远端文件）、--force-overwrite 是全局放行。
                _UI.line("     python3 push_api.py --reset-baseline --yes  # 重建基线（本地与远端已无法对齐时）")
                _UI.line("       · 上面两条都不成立时的解套方式；会丢弃现有基线记录，")
                _UI.line("         重建后「远端文件是否被他人改过」暂时失去参照")
                _UI.line("       · ⚠ 不是万能解套：本地与远端**仍有不一致**时，")
                _UI.line("         --reset-baseline 与 --init-baseline 走同一个函数，")
                _UI.line("         同样会拒绝建基线（实测返回 None），synced 依旧为空")
            _UI.line("     --force-overwrite                 # 全局放行，会回退远端更新，仅在你确知后果时用")
            return Verdict(
                BLOCK, self.id, action="return",
                reason=("基线未记录本地所基于的远端版本" if synced is None
                        else f"本地副本落后：基线 {synced[:8]}，远端 {head_sha[:8]}"),
                remedy=(["--mark-synced（确认本地已含远端全部改动，首选）",
                          # N590（A153，中危）：这一条原先与下面的
                          # `not_recommended` **逐字相同**（A153 实测交集 = 1）。
                          #
                          # 即：同一个动作、同一句话，同时出现在「建议的
                          # 出路」和「明确不该建议的操作」两个语义相反的
                          # 字段里。结构化消费者按 remedy 去执行，拿到的是
                          # 脚本自己标注为不该做的那条 —— 而
                          # `not_recommended` 的字段注释原话正是
                          # 「把它们列进 remedy 等于把用户推进死循环」。
                          #
                          # 修法不是删掉任何一方：`--pull` 在本场景下
                          # 仍是很多人会试的第一反应（不列等于不说），
                          # 风险也确实要说。改成**分工**：remedy 只列动作
                          # 并指向不推荐项，风险说明由 not_recommended
                          # 承载，两集合逐字不相交。
                          "--pull（本状态下有风险，见 not_recommended）",
                          "--reset-baseline（上面两条都不成立时的解套方式）",
                          "--force-overwrite（确知后果时）"] if synced is None else
                         ["--pull（三方合并主干改动到本地，首选）",
                          "--mark-synced（确认本地已含远端全部改动）",
                          "--force-overwrite（确知后果时）"]),
                # M127（A117）：与 N486 完全同形态 —— 脚本**知道**
                # `--pull` 在这个场景下不解除本层，却只把警告写进了
                # remedy **字符串的括号里**。结构化消费者拿到的是一整条
                # 字符串，无法区分「建议」与「不推荐」。
                #
                # RewindGuard 那一处（not_recommended=["--pull",
                # "--mark-synced"]）已经用上了这个字段，且它的注释恰好
                # 点名了同样的两条 —— 同一份机制，一处显式标注、一处塞
                # 进字符串，就是覆盖不全。
                #
                # 只在 synced is None 时置：`--pull` 对"落后但基点可信"
                # 那一支是**首选**（首选 remedy 第一条），标成不推荐会把
                # 唯一的正确出路也否掉。
                not_recommended=(["--pull（本状态无可信基点，"
                                  "可能产生假冲突标记且不解除本层）"]
                                 if synced is None else []))
        _UI.warn(f"\n  ! 本地副本落后/未声明（{str(synced)[:8] if synced else '未声明'} → {head_sha[:8]}），"
                f"--force-overwrite 已放行，存在回退远端更新的风险")
        return Verdict(
            BLOCK, self.id, action="return",
            reason=f"本地副本落后（{str(synced)[:8] if synced else '未声明'} → {head_sha[:8]}）",
            remedy=["--pull", "--mark-synced"], bypassed=True)


class FileBaselineGuard(Guard):
    """第一层：文件级基线校验（mode + sha 一起比）。

    远端某文件必须等于「我们上次推送后记下的状态」，否则说明远端被别的
    通道改过。比 (mode, sha) 而非只比 sha：只比 sha 会让「只改了可执行位」
    的变更被判为「内容已一致」而跳过，改了等于没改（T-11）。

    这道 Guard 同时是**分流器**：把待推集合划成 todo / blocked / risky，
    结果写回 ctx。它不做「是/否」判定，所以 BLOCK 只在明确要中止时出现。
    """

    id = "file_baseline"
    after = ("stale_local",)
    bypassable = True

    def check(self, ctx):
        state = ctx.state
        lmap = ctx.lmap
        rstate = ctx.rstate
        force = ctx.force
        allowed = ctx.allowed
        todo, blocked = [], []
        risky = list(ctx.get("risky") or [])
        for rel in ctx.files:
            lmode, lsha = lmap.get(rel, ("100644", ""))
            rmode, rsha = rstate.get(rel, (None, None))
            base = state["files"].get(rel)
            # 变量名必须避开 base_sha —— 那是本次推送的 ref 基点（见第二层乐观锁），
            # 同名覆盖会让它变成「基线里记的文件 sha」，乐观锁随即误报并发并中止。
            base_mode = base.get("mode") if isinstance(base, dict) else None
            base_rec_sha = base.get("sha") if isinstance(base, dict) else base

            if (lmode, lsha) == (rmode, rsha):
                _UI.line(f"  · {rel} 已与远端一致，跳过")
                continue
            ok = force or rel in allowed
            if base is None:
                if rsha is None and not ok:
                    # 基线没记录**且远端也没这个文件** → 纯新增，不存在覆盖风险。
                    # 只有当「基线没记录、远端却有」时才真的无法确认原状态，那才要拦。
                    todo.append(rel)
                    _UI.line(f"  + {rel} 新增文件（远端不存在）")
                    continue
                if ok:
                    todo.append(rel)
                    risky.append(rel)
                    _UI.warn(f"  ! {rel} 基线未记录（已放行，将整文件覆盖，远端原内容丢失）")
                else:
                    blocked.append((rel, "基线里没有这个文件、远端却已存在，无法确认远端原状态"))
                continue
            if (rmode, rsha) != (base_mode, base_rec_sha):
                if ok:
                    todo.append(rel)
                    risky.append(rel)
                    _UI.warn(f"  ! {rel} 远端已被改动（已放行，将整文件覆盖）")
                    # N424（第一百三十六轮，中危）：放行路径**整个绕过**了
                    # 下面那套三分类 —— ok 为真 → 不进 blocked →
                    # `if not blocked: return`，三分类连入口都到不了。
                    #
                    # 于是 P0-1 专门要防的「静默回退」在 --force-file 与
                    # --force-overwrite 两条路径上**拿不到任何专门警告**，
                    # 只剩上面那句通用的「已放行，将整文件覆盖」——
                    # 而这两条恰恰是脚本自己推荐给用户的出路
                    #（--force-file 还是「细粒度」的那一个）。
                    #
                    # 这里补上同一个判据（与三分类同源，不另起一套）：
                    # 本地 == 基线 → 你没改过 → 这是**回退**，不是覆盖冲突。
                    if _local_matches_baseline(state, lmap, rel):
                        _UI.error(
                            f"     ⚠ 你没改过 {rel}（本地 == 基线），"
                            f"推送后 {BRANCH} 会被**回退**到你的旧内容。")
                        _UI.line("       这不是冲突，是覆盖：GitHub 会判定"
                                 "「你故意回退」并直接采纳，不报错。")
                        _UI.line("       放行是你指定的，但后果需要你知道；"
                                 "想避免就先跑 --pull。")
                else:
                    detail = f"远端 {str(rsha)[:8]}/{rmode} ≠ 基线 {str(base_rec_sha)[:8]}/{base_mode}"
                    blocked.append((rel, f"{detail}，可能被他人改动"))
                continue
            todo.append(rel)

        ctx.todo, ctx.blocked, ctx.risky = todo, blocked, risky
        if not blocked:
            return Verdict(PASS, self.id)

        if ctx.wf == "pr":
            # 主干这些文件动过 ≠ 不能推：推的是任务分支，GitHub 合并时才判定。
            #
            # 但「不会静默覆盖」这句话只在**本地确有改动**时成立。
            # 若本地版本 == 基线（只是落后），三方合并时 theirs==base、
            # ours 变了 → GitHub 直接采纳 ours，等于把主干回退到你的旧版本，
            # 且不会报错。这正是 P0-1 想防的情形，必须单独、明确地警告。
            # 三分类，不能只分两类。
            #
            # _local_matches_baseline() 在**基线缺失**时返回 False（保守），
            # 但调用方把 False 一律读成「本地确实改过」→ 归入 overlap
            # → 下面无条件放行。于是「基线没记录、远端却有、本地其实是
            # 旧版本」的文件会被当成你的改动推进主干，静默回退主干。
            #
            # 基线缺失必须**单独成类**：那不是「已知本地改过」，
            # 是「无法证明本地改过」。二者后果相反，不能混。
            revert, unrecorded, overlap = [], [], []
            for rel, why in blocked:
                base = (state.get("files") or {}).get(rel)
                if not isinstance(base, dict) or not base.get("sha"):
                    unrecorded.append((rel, why))
                elif _local_matches_baseline(state, lmap, rel):
                    revert.append((rel, why))
                else:
                    overlap.append((rel, why))

            if revert:
                _UI.error(f"\n❌ 以下文件你没改过、本地仍是旧版本，"
                        f"推送后 {BRANCH} 会被**回退**到你的旧内容：")
                for rel, why in revert:
                    _UI.line(f"   - {rel}: {why}")
                _UI.line("\n   这不是冲突，是覆盖：GitHub 会判定「你故意回退」并直接采纳。")
                _UI.line("   先跑 --pull 把主干改动合进本地，再推送。")
                if not ctx.bypass:
                    _cancel_blocked("revert_local_head")
                # N424：原先这里还有一行声称「--force-overwrite 已放行，
                # 这些文件会被回退」，它是**双重假话**：
                #   ① 不可达 —— 进入本分支的前提（blocked 非空）蕴含
                #      ok 为假 → force 为假 → ctx.bypass（≡ force）为假，
                #      上一句必然已经 raise。
                #   ② 即便可达也是假话 —— 下面只有
                #      `for rel, _ in overlap: todo.append(rel)`，
                #      revert / unrecorded 两类从不追加，文件根本不会被推，
                #      「将被回退」无从谈起（实测 ctx.todo 为空）。
                # 已删除。真正的放行路径警告见本函数上半部分的 N424 修补。

            if unrecorded:
                _UI.error("\n❌ 以下文件基线未记录、远端却已存在，"
                        "无法判断本地是「你改过」还是「只是落后」：")
                for rel, why in unrecorded:
                    _UI.line(f"   - {rel}: {why}")
                _UI.line("\n   先跑 --pull 补齐基线（通常一行就好）；")
                _UI.line("   确知本地确实改过时，用 --force-file <文件> 逐个点名放行。")
                if not ctx.bypass:
                    # 与 revert 同级处理：证明不了「本地改过」就不能放行，
                    # 否则就是拿主干冒险。bypass 时给出明确警告。
                    _cancel_blocked("unrecorded_baseline")
                # N424：原有一行「已放行，将按本地内容覆盖（无法排除是
                # 回退）」，与上面那行同理：不可达（bypass ≡ force，
                # 与 blocked 非空互斥），且即便可达也是假话
                #（unrecorded 类从不进 todo）。已删除。

            if overlap:
                _UI.warn(f"\n  ! 以下文件在 {BRANCH} 上已有新改动，但你本地也改过"
                        f"（合并时可能冲突，不会静默覆盖）：")
                for rel, why in overlap:
                    _UI.line(f"   - {rel}: {why}")
                _UI.line("   建议先跑 --pull 把主干改动合进本地，PR 会干净很多。")

            # 只有「本地也改过」的才放行；回退类的已在上一步拦下或明确警告。
            for rel, _ in overlap:
                todo.append(rel)
            ctx.todo = todo
            return Verdict(PASS, self.id)

        _UI.error("\n❌ 以下文件可能覆盖他人改动，已中止：")
        for rel, why in blocked:
            _UI.line(f"   - {rel}: {why}")
        _UI.line("\n   正确做法：python3 push_api.py --pull        # 三方合并远端改动到本地")
        _UI.line("   确知要覆盖某个文件时，用细粒度放行（别用全局 --force-overwrite），")
        _UI.line("   并显式列出要推的文件（否则其它被拦的文件会一并中止本次推送）：")
        _UI.line("     python3 push_api.py --force-file 可疑文件 要推的文件")
        _cancel_blocked("overlap_blocked")


class SizeGuard(Guard):
    """大文件预检。

    放在 dry-run **之前**：预演的意义就是提前暴露这种阻塞性错误，
    排在后面会导致「--dry-run 一切正常，真推时才报 12MB 超限」。
    dry-run 时只警告不中止，让预演能继续走完流程。
    """

    id = "oversize_blob"
    after = ("file_baseline",)
    bypassable = False

    def check(self, ctx):
        oversize = []
        for rel in ctx.todo:
            # 必须兜住 OSError：仓库里可以有指向不存在目标的符号链接
            # （git 允许提交这种 blob），os.path.getsize 会抛 FileNotFoundError。
            # 预览那里（见 preview）已经兜了，这里漏了就会裸 traceback，
            # 而且此时 blob 已建、分支已建、状态未落盘 —— 留下脏远端状态。
            # symlink 用 lstat：要的是链接自身长度，不是跟随后的目标大小。
            full = os.path.join(ROOT, rel)
            try:
                size = (os.lstat(full).st_size if os.path.islink(full)
                        else os.path.getsize(full))
            except OSError:
                size = 0
            if size > MAX_BLOB_BYTES:
                oversize.append((rel, size))
        if not oversize:
            return Verdict(PASS, self.id)
        for rel, size in oversize:
            _UI.error(f"\n❌ {rel} 有 {size / 1024 / 1024:.1f} MB，超过 "
                    f"{MAX_BLOB_BYTES // 1024 // 1024} MB 上限。"
                    f"base64 后还会再膨胀约 33%，容易超时/被拒。"
                    # N486（A117）—— **人类文案那一半**：下面 Verdict 的
                    # remedy / not_recommended 已改（不再把「改用 Git LFS」
                    # 当出路），而**这句给用户读的话**原样没动，仍然写着
                    # 「请改走 Git LFS」。只改结构化字段不改文案 = 改动只对
                    # `--json` 消费者生效，终端前照着做的人读到的还是死路。
                    # 实测闭环：交给 LFS 后再推 →「以下文件由 Git LFS 管理，
                    # 本脚本拒绝推送」，且连推三次都一样（永久，非一次性）。
                    f"请用 git 命令行推送这些文件。\n"
                    f"   （不要改用 Git LFS：交给他管之后本脚本将**永久**"
                    f"拒绝推送该文件。）")
        _names = ", ".join(p for p, _ in oversize[:5])
        if len(oversize) > 5:
            _names += f" 等 {len(oversize)} 个"
        _detail = "\n".join(
            f"{p} 有 {s / 1024 / 1024:.1f} MB，超过 "
            f"{MAX_BLOB_BYTES // 1024 // 1024} MB 上限"
            for p, s in oversize)
        # N589（A153，中危）：dry 下不能返回空 Verdict。
        #
        # 旧写法 `return Verdict(WARN, self.id)` 在 `_names` / `detail` /
        # `oversize` **全都算出来之前**就返回了 —— 于是文件名、字节数、
        # 个数一个都没有，只有 `level: warn`。
        #
        # 而本 Guard 的 docstring 第一句就是「放在 dry-run **之前**：预演的
        # 意义就是提前暴露这种阻塞性错误」—— **dry 是它唯一被设计要服务
        # 的场景**，结构化字段却全加在它走不到的那一半。
        #
        # 后果有方向性：`level: warn` 读作「没有阻塞性问题」，而真推时是
        # `block`。预演与真推结论相反，恰恰毁掉预演的意义。
        # 仍返回 WARN（不中止预演，这是既有且正确的取舍），只把信息补全。
        if ctx.get("dry"):
            return Verdict(WARN, self.id,
                           reason=(f"{len(oversize)} 个文件超过 "
                                   f"{MAX_BLOB_BYTES // 1024 // 1024} MB 上限"
                                   f"（{_names}）"),
                           detail=_detail,
                           remedy=[f"用 git 命令行推送（这些文件本脚本推不了："
                                   f"交给 LFS 后也会被拒）",
                                   "或从本次推送中排除这些文件"],
                           not_recommended=["改用 Git LFS（交给他管之后，"
                                            "本脚本将**永久**拒绝推送该文件，"
                                            "只能走 git 命令行）"],
                           oversize=[{"path": p, "bytes": s}
                                     for p, s in oversize])
        return Verdict(
            BLOCK, self.id, action="abort",
            # N487（A117）：reason 原先只有计数，文件名与字节数只存在于
            # 人类文案（text）里。`_enforce` 的注释写明「优先用 detail：
            # 它是完整文案；reason 只是一句话摘要」，而本调用点**根本
            # 没传 detail** —— 于是结构化消费者只能拿到计数。
            # 补 detail（完整文案）并把文件名带进 reason。
            reason=(f"{len(oversize)} 个文件超过 "
                    f"{MAX_BLOB_BYTES // 1024 // 1024} MB 上限，已中止"
                    f"（{_names}）"),
            detail="\n".join(
                f"{p} 有 {s / 1024 / 1024:.1f} MB，超过 "
                f"{MAX_BLOB_BYTES // 1024 // 1024} MB 上限"
                for p, s in oversize),
            # N486（A117，中危）：原 remedy 第一条是「改用 Git LFS」，
            # 而 LFS 拒绝那一侧（_lfs 相关检查）的原话是
            #   「请用 git 命令行推送这些文件（脚本也提过：大文件应改走 Git LFS）」
            # —— 即：脚本**知道**照做之后自己会永久拒推该文件，却把这条
            # 警告写在了**另一侧的括号里**，没回头改这里的 remedy。
            #
            # 实测同一仓库连推三次（不重建基线）：
            #   未交给 LFS → 第1/2/3次都是「N 个文件超过 10 MB 上限，已中止」
            #   交给 LFS   → 第1/2/3次都是「以下文件由 Git LFS 管理，本脚本拒绝推送」
            # 闭环确认：照 remedy 做之后是**永久**推不了，不是一次性。
            #
            # 把它列进 remedy 等于把用户推进死循环 —— 这正是
            # `not_recommended` 字段存在的理由（Verdict 的注释原话）。
            remedy=[f"用 git 命令行推送（这些文件本脚本推不了："
                    f"交给 LFS 后也会被拒）",
                    "或从本次推送中排除这些文件"],
            not_recommended=["改用 Git LFS（交给他管之后，本脚本将"
                             "**永久**拒绝推送该文件，只能走 git 命令行）"],
            oversize=[{"path": p, "bytes": s} for p, s in oversize])


GUARDS = [
    UnresolvedConflictGuard(),
    RewindGuard(),
    StaleLocalGuard(),
    FileBaselineGuard(),
    SizeGuard(),
]

# 推送前**连续**跑的那几道：它们之间没有任何业务逻辑，可以放进一个循环。
# FileBaseline / Size 必须留在原处 —— 它们夹在「建分支 / 预览 / 拿 lmap」
# 之间，那些是真逻辑，不能提上来。
PRE_PUSH_GUARDS = [g for g in GUARDS if g.id in
                   ("unresolved_conflict", "rewound_local_head", "stale_local")]


def validate_guard_order(guards=None):
    """校验 GUARDS 列表满足每个 Guard 声明的 after 依赖。

    返回 [(guard_id, 问题描述)]；空列表表示顺序合法。

    这是本阶段的核心产出：把「顺序只存在于注释里」变成**跑得起来的断言**。
    新增 Guard 时忘了声明依赖、或手滑调换列表顺序，这里会直接报出来 ——
    而不是等某天防护静默失效了才发现。
    """
    guards = GUARDS if guards is None else guards
    ids = [g.id for g in guards]
    problems = []
    seen = set()
    for g in guards:
        for dep in getattr(g, "after", ()) or ():
            if dep not in ids:
                problems.append((g.id, f"依赖的 Guard 不存在：{dep}"))
            elif dep not in seen:
                problems.append(
                    (g.id, f"声明必须在 {dep} 之后，但 {dep} 在列表里排在它后面"
                           f"（当前顺序：{ids}）"))
        seen.add(g.id)
    # A4-H3 同族（`for i in set(ids) if ids.count(i) > 1` 是 O(n²)，
    # 且"推导式内重建集合"这个写法本身就是 A4-H3 那条判据要盯的形态）：
    # 一次遍历分出重复，不重建集合、不做逐元素计数。
    # 本函数跑在每次推送前的 Guard 顺序校验上，n 是 Guard 个数（十几），
    # 规模上不致命 —— 改它是为了让那条「推导式内不重建 set」的判据
    # 重新成立，而不是等它继续红着没人看。
    _id_seen, dup = set(), []
    for i in ids:
        if i in _id_seen and i not in dup:
            dup.append(i)
        _id_seen.add(i)
    for i in sorted(dup):
        problems.append((i, "id 重复"))
    return problems


def run_guards(ctx, guards=None):
    """跑推送前的连续 Guard。返回被拦下的 Verdict，或 None。

    放行（--force-overwrite）时 Guard 自己已打印警告，这里继续跑下一道 ——
    不是放行整个推送，只放行这一道判据。
    """
    _seen = []
    for g in (PRE_PUSH_GUARDS if guards is None else guards):
        # N343：Guard 的输出**整段保留**，不受 --quiet 影响。
        # 见 ui_keep_all 的说明 —— 被拦下时没有一行是进度噪音。
        #
        # 放在**循环内**而不是整个 run_guards 外面：g.check 里可能
        # 抛 GuardBlock（UnresolvedConflictGuard / SizeGuard 走 raise），
        # 用 with 的 try/finally 才能保证 quiet 一定还原。
        with ui_keep_all():
            v = g.check(ctx)
        _seen.append({"guard": g.id, "level": v.level,
                      "action": getattr(v, "action", None)})
        if v.level == BLOCK and not (g.bypassable and ctx.bypass):
            # 记进审计流水：被哪道 Guard 拦下、放行了几道，是事后复盘
            # 「为什么这次没推上去」的唯一凭据。stdout 一滚就没。
            # M168（A153）：同上，补 reason / detail。
            # 这三道（unresolved_conflict / rewound_local_head /
            # stale_local）走 run_guards，另两道走 _enforce_audited ——
            # 两条写入路径都缺，所以两处都要补。
            audit({"kind": "guard_block", "guard": g.id,
                   "level": v.level, "passed": _seen,
                   "reason": getattr(v, "reason", ""),
                   "detail": getattr(v, "detail", "")})
            return v
    audit({"kind": "guard_pass", "passed": _seen})
    return None

def _main(opts, explicit):
    dry, yes = opts["dry"], opts["yes"]

    # -------- git 可用性校验（必须最先）--------
    # git 不可用时 detect_changes()=[]、local_state_map()={}，脚本会把
    # 「读不到仓库」伪装成「仓库是干净的」，最终打印「本地没有待推送的
    # 改动」—— 用户以为没事，改动一个都没推（--direct 模式实测复现）。
    # 必须在**任何**其它动作之前拦下，越晚越容易让人误以为推送成功。
    require_git_repo()

    # -------- --dry-run 门禁（必须前置）--------
    # 原来 dry 只在两处被检查：建分支前（2478）和推送前（2604 附近）。
    # 而**子命令全在它之前分发**：--delete-branch 会真的删远端分支，
    # --pull 会改写本地文件，--merge 会真的合并进主干。
    # 实测：`--dry-run --delete-branch task/x --yes` 分支当场消失 ——
    # dry-run 是探索陌生命令时的第一反应，而删除远端分支**不可逆**。
    #
    # 判据用「白名单剩余项」而不是逐个 if dry：新增子命令时忘了同步
    # 这里，会静默漏过门禁。所以列出所有会改动的项，命中即拒绝。
    if dry:
        MUTATING = ("init", "reset", "mark_synced", "pull", "resolve",
                    "merge", "close_pr", "delete_branch",
                    # N116：--clean-conflicts 会**真的删除本地文件**
                    # （.push-conflicts 下的副本）。它不在原白名单里，
                    # 于是 --dry-run --clean-conflicts 照删不误 ——
                    # 而 dry-run 是探索陌生命令时的第一反应。
                    "clean_conflicts")
        hit = [k for k in MUTATING if opts.get(k)]
        if hit:
            raise SystemExit(
                f"--dry-run 与 {', '.join('--' + k.replace('_', '-') for k in hit)} "
                f"不能同时使用。\n"
                f"  这些子命令会真实改动远端或本地文件，预演模式下无法模拟。\n"
                f"  --dry-run 只支持默认推送、--status 与 --prune。")
    init, reset, force = opts["init"], opts["reset"], opts["force"]
    global _NO_DELETE, _ENABLE_DELETE, _DRY_RUN
    _DRY_RUN = bool(opts.get("dry"))
    # N246：同步到模块级，让运行出口（finally）也能判断。
    global _DRY_THIS_RUN
    _DRY_THIS_RUN = _DRY_RUN
    _NO_DELETE = bool(opts.get("no_delete"))
    _ENABLE_DELETE = bool(opts.get("enable_delete"))
    # 命令行开关刚定下来，必须**重新绑定**：末尾那次只看环境变量，
    # 不重绑的话 --delete 这次不会生效（详见 _bind_removals 的说明）。
    _bind_removals()
    # N112：set_ui 在**生产路径零调用**（只有测试调过），
    # 于是 Ui 恒为 text 模式、Runtime.ui 是从未赋值的死字段，
    # --json 实际只影响几处 if 判断，统一出口从未生效。
    # 开关解析完、分派之前绑定一次。
    #
    # --mine 也要在这里登记：它和 --json 是**同一类**输出契约
    #（stdout 只给机器消费），但走的不是 Ui 通道 —— show_scope 的
    # only_mine 分支直接 print 路径。所以 Ui.mode 反映不出它，
    # 必须单独立一个标志，否则分派前那些 `if not opts.get("json")`
    # 的静音判断会把它漏掉（实测：--mine 的管道里混进了
    # 「远端 main = …」和 core.fileMode 告警两行，被 while read 当成
    # 两个不存在的文件名）。
    global _STREAM_MODE
    _STREAM_MODE = bool(opts.get("mine"))
    if opts.get("json") or opts.get("quiet"):
        set_ui(mode="json" if opts.get("json") else None,
               quiet=bool(opts.get("quiet")))
        try:
            get_ui().json_warned = bool(opts.get("json"))
        except BaseException:
            pass

    # N336（第一百〇五轮，中危）：这两个纯本地命令**断网时全部退出码 1**。
    #
    # N314 只修了一半。它补的是"取树"（remote_state_map），而 `ref` 这一步
    # 在它**之前**、无条件执行 —— 取不到就 SystemExit。代码自己在上面写着
    # 「那两步的离线可用性另案」，这就是那"另案"。
    #
    # 实测（同一仓库、断网）：
    #     --prune             退出码=1  首行=curl 失败（退出码 7）
    #     --status            退出码=1  首行=curl 失败（退出码 7）
    #     --audit             退出码=1  首行=curl 失败（退出码 7）
    #     --clean-conflicts   退出码=1  首行=curl 失败（退出码 7）
    #   ★ 四个命令全部 1，N314 那句"--prune 断网时能跑、--audit 不能"
    #     的对照关系在当前代码里**不成立**（豁免名单只影响取树，
    #     而取树根本没机会执行到）。
    #
    # 而 head_sha 对这两个命令**毫无用处**：
    # show_audit(limit=...) 与 clean_conflict_artifacts(state, yes=...)
    # 都不收它。所以直接跳过即可，不需要"取不到就降级"的兜底逻辑 ——
    # 少一条路径就少一处漂移。
    _LOCAL_ONLY = (opts.get("audit") or opts.get("clean_conflicts"))
    if _LOCAL_ONLY:
        ref, head_sha = {}, None
    else:
        ref = api("GET", f"/git/refs/heads/{BRANCH}")
        head_sha = ref.get("object", {}).get("sha")
        if not head_sha:
            raise SystemExit(f"取不到远端 {BRANCH}：{json.dumps(ref)[:300]}")
    # N99：--status --json 的输出不是合法 JSON，因为这里无条件 print。
    # show_status 内部已经专门处理过同类问题（把 _report_conflict_artifacts
    # 捕获进 warnings），但只修了它自己那一处 —— 分派**之前**的这些
    # print 照样插在 JSON 前面。
    #
    # 判据：纯数据流模式（--json / --mine）下分派前的任何输出都必须静音。
    # 用 opts 直接判而不是等 set_ui：这段代码在 set_ui 之前执行。
    # --mine 与 --json 同属一类（见 _stream_active 的说明）：管道里混进
    # 这行会被 while read 当成文件名。
    if not (opts.get("json") or opts.get("mine")):
        # M61（A70）：`--audit` / `--clean-conflicts` 走 `_LOCAL_ONLY` 分支，
        # head_sha **刻意**就是 None（N336 论证过它们不需要远端），
        # 于是这一行打成「远端 main = None」—— 出现在**输出的第一行**。
        #
        # 用户看到"远端 = None"的自然反应是"连不上远端"，而实际上
        # 这两个命令**根本没去连**。把"没取"显示成"取到了一个 None"，
        # 排查方向被指向了相反的一侧（与 M57 那类"文案引错方向"同族）。
        #
        # 不取就不打印这一行；确实取到（或确实取失败）时才显示。
        if head_sha:
            print(f"远端 {BRANCH} = {head_sha}")
        elif not _LOCAL_ONLY:
            # 取失败已在上面 raise，这里理论不可达；留着是防止将来
            # 把 raise 改成降级后，这一行又变成静默的 "None"。
            print(f"远端 {BRANCH} = 取不到（{str(head_sha)}）")

    # N71：CONFLICT_DIR 由编译期 ROOT 算出且从不刷新 —— 是唯一一个
    # 没跟着配置走的模块级路径。用 --root 推另一个仓库时，冲突副产物
    # 仍写进**原来那个仓库旁边**的目录：跨仓库混用（且里面是远端明文）。
    _refresh_conflict_dir()

    # 启动即探测一次：本脚本存在的唯一理由是「github.com 不通」。
    # 哪天这个前提不成立了，必须让人第一时间知道 —— 否则用户会在
    # 明明能 git push 的环境里，继续养着这套 5500 行的自造防护。
    #
    # 只在「git 其实通」时才出声：被拦是本环境常态，每次都提示等于
    # 刷屏，久了就没人看，真正重要的那条（通了）反而被淹没。
    if probe_git_transport().get("state") == "ok" and not opts.get("json"):
        print(f"\n{'=' * 62}")
        print(f"  ⚠️  {_transport_note()}")
        print(f"{'=' * 62}")

    # N68：prune / show_status / close_pr 三个子命令**不需要**远端文件树，
    # 却被这行无条件拉取挡住。而 remote_state_map 在树被截断时直接中止：
    #   if tree.get("truncated"): raise SystemExit("远端 tree 被截断…")
    # 于是大仓库（超过 GitHub recursive tree 上限）上这三个只读/体检命令
    # **完全不可用** —— 它们本来根本用不到那棵树。
    #
    # 改为惰性：需要时才拉。三个子命令据此绕开这层前置。
    #
    # N314（第九十四轮，中危）：豁免名单漏了 **--audit** 与
    # **--clean-conflicts** —— 两个纯本地命令（前者读本地流水、
    # 后者只删本地副产物），却要拉一次完整远端文件树。
    #
    # 差别只在名单里有没有它：--prune 断网时能跑，--audit 不能。
    # 而这两个**恰恰是离线排查唯一可用的工具** ——
    # show_audit 的 docstring 写的正是它的存在理由：
    # 「--status 看的是『现在的状态』，--audit 看的是『发生过什么』，
    #   排查『上周那次到底推上去没有』只能靠后者。」
    # 断网 = 最需要排查的时候，工具自己用不了。
    #
    # 注：N336 已把 `ref` 这一步也纳入豁免（纯本地命令跳过它），
    # transport 探测（见其 docstring）**永不抛异常**、失败按"未知"继续。
    # 所以到这里，两个纯本地命令断网时确实可用了。
    #
    # （N314 当年写的"--prune 断网时能跑、--audit 不能"这句对照，
    #  在 ref 未豁免时**并不成立** —— 实测四个命令断网全部退出码 1。
    #  那条注释描述的差异只在取树，而取树根本没机会执行到。
    #  与 N277 同一教训的第三次：两条修复必须一起做，只做一条等于没做。）
    _NEED_RSTATE = not (opts.get("prune") or opts.get("status")
                        or opts.get("close_pr") or opts.get("audit")
                        or opts.get("clean_conflicts")
                        or opts.get("scope") or opts.get("mine"))
    rstate = {} if not _NEED_RSTATE else remote_state_map(head_sha)
    if _NEED_RSTATE:
        # M86（A87，P2）：这一行是**所有早退路径共有的前置输出** ——
        # 它在分派之前无条件 print，于是哪怕下面的分支吐出了合法 JSON，
        # 前面多这一行也会让整体**不可解析**（实测三场景全部 ★）。
        # 两条修复互相抵消，与 N308 / M42 同族。
        #
        # N99 立的判据「JSON 模式下分派前的任何输出都必须静音」是对的，
        # 问题只在实现覆盖了它眼前那一处（M80 修的 state is None），
        # 没覆盖这一处。
        if not (opts.get("json") or opts.get("mine")):
            print(f"远端文件 {len(rstate)} 个")
    # N324：把 legacy 基线的告知渠道补进**推送路径**。
    # 它原先只在 --status 的配置展示里打印，于是用户在 push 时看到的
    # 是「✅ 推送完成」，不知道自己正在用一份可能与本仓库无关的基线
    # （那份是编译期写死的绝对路径，不含 ROOT/仓库名）。
    if _LEGACY_STATE_ACTIVE:
        _old, _new = _LEGACY_STATE_ACTIVE
        print(f"\n  ! 正在沿用仓库外的旧基线 {_old}（与当前仓库无关）。")
        print(f"     迁移：cp {_old} {_new}")
    # N59：remote_state_map 已有的防护只覆盖「取树失败」（响应无 tree 字段），
    # **不覆盖「树取到了但里面没有 blob」**（tree: [] 或全是 commit/tree 项）
    # —— 那是合法响应，会静默返回 {}。
    #
    # 而 _main 全文此前没有 `if not rstate` 的整体防护。空 rstate 与
    # N32（远端无此文件→跳过）叠加会反转语义：基线里每个文件都判成
    # "远端没有"，若后续判据再放松就是整仓删除。
    # 实测：rstate={} + 基线 19 个文件 → entries=19、abort=False。
    #
    # 基线非空却拿到空 rstate，只有两种解释：远端真的空了（那基线也该空），
    # 或取树出了问题。无论哪种，继续删都不安全。
    # N59 检查见下方（state 加载之后）

    # init/reset 用非严格模式：旧基线坏了也要能重建（否则用户得先手工删文件）。
    state = load_state(
strict=not (init or reset))

    # -------- N59：远端文件树为空但基线非空 --------
    # remote_state_map 已有的防护只覆盖「取树失败」（响应无 tree 字段），
    # **不**覆盖「树取到了但里面没有 blob」（tree: [] 或全是 commit/tree 项）
    # —— 那是合法响应，会静默返回 {}。
    #
    # 空 rstate 与 N32（远端无此文件→跳过）叠加会反转语义：基线里每个文件
    # 都判成"远端没有"，后续判据再放松就是整仓删除。
    # 实测：rstate={} + 基线 19 个文件 → entries=19、abort=False。
    #
    # 基线非空却拿到空 rstate，只有两种解释：远端真的空了（那基线也该空），
    # 或取树出了问题。无论哪种，继续删都不安全。
    # 只在**推送路径**上拦。--merge / --pull / --prune 等子命令要么只读、
    # 要么自己另取远端状态，用空 rstate 跑是它们正常的构造方式
    # （测试就常这么注入）。误拦会把无关命令全挡住。
    _is_push = not any(opts.get(k) for k in (
        "prune", "status", "close_pr", "merge", "pull", "resolve",
        "mark_synced", "delete_branch", "audit", "clean_conflicts",
        "new_task", "init", "reset"))
    if _is_push and _NEED_RSTATE and not rstate and ((state or {}).get("files")):
        # M75（A79）：同上，语义是"拦下"而非"出错" —— 上面那段注释
        # 论证的正是"继续删不安全"，所以这里是**保护性中止**；
        # 改动一个没丢，处置方式是 --init-baseline 重建基线。
        # 按 N361 的契约应给退出码 2（调用即抛，不写 raise）。
        _blocked(
            "远端文件树为空，但基线里记着 "
            f"{len((state or {}).get('files') or {})} 个文件"
            " —— 两者不可能同时成立。\n"
            "   可能原因：取远端树失败却被当成空结果、或 --root 指向了错误目录。\n"
            "   已中止以避免误判成「远端全都没有」而批量删除。\n"
            "   确认远端确实是空的：--init-baseline 重建基线。")

    # -------- 基线建立 / 重设 --------
    # N299（第八十八轮，中危）：这两个**纯本地**命令不需要基线 ——
    # `--audit` 只读本地审计流水（audit_read(audit_path())）；
    # `--clean-conflicts` 只读 CONFLICT_DIR，且 state 是可选参数
    # （`state or {}`）。基线缺失时它们的语义也是自洽的：
    # clean 的 pending 为空 → 没有"已知未解决冲突" → 全部可清理。
    #
    # 原先闸门对它们一视同仁地 return，实测（只删掉基线文件）：
    #   · --audit           → 5 条真实流水**一条都看不到**
    #   · --clean-conflicts → 含远端明文的两个副本**一个都清不掉**
    #
    # 三条后果：
    #   ① 排查能力在**最需要它的时刻**失效 —— show_audit 的 docstring
    #      原话：「排查『上周那次到底推上去没有』只能靠后者」。
    #      而基线丢失恰恰是最想先查历史的时刻（换机器、误删基线、
    #      准备 --reset-baseline 前想核对）。
    #   ② 提示给的出路与排查目标**相反**：让用户去跑 --init-baseline，
    #      而重建基线会冲掉那份正是排查对象的参照 —— 形成死锁
    #      「想查历史 → 被要求先重建基线 → 历史参照被冲掉」。
    #   ③ 敏感残留清不掉：--clean-conflicts 的存在理由（其 docstring）
    #      是"被遗忘的**远端明文副本**一旦产生就永久留下"，
    #      而基线缺失恰恰是最可能遗留副产物的场景。
    #
    # 定中危而非高危：不丢数据、不误删，是"能力不可用"而非"做错事"。
    #
    # 让它们**根本不进这道闸门**，直接走到下面**原有的**分派区 ——
    # 刻意不复制一份调用（两份实现迟早漂移，正是这批报告反复批评的形态）。
    #
    # 注意：必须在外层 condition 上排除，不能在内层 `pass`。
    # 内层 pass 会 fall through 到下面的 `init_baseline(...)` 并 return
    # —— 实测就是这么错的：基线被自动写了一份，而 --clean-conflicts
    # 压根没执行（输出只有"基线已写入"）。
    _NO_BASELINE_OK = (opts.get("audit") or opts.get("clean_conflicts"))
    if init or reset or (state is None and not _NO_BASELINE_OK):
        if state is None:
            if not (init or reset):
                # M80（A84，P2）：这里无条件 print，违反的正是脚本自己
                # 立的判据 —— N99 在分派前那处写「JSON 模式下分派前的
                # 任何输出**都必须静音**」并加了 `if not opts.get("json")`。
                # **那一条只修了它眼前那一处**，这里没跟着修
                # （"修一处 ≠ 修全部"，与 N348 / M50 同族）。
                #
                # 而这条路径**首次使用必然命中**：
                #     $ push_api.py --status --json      ← 空仓库、无基线
                #     rc=0、⚠️ 文案 + 误报"不支持"、json.loads 失败
                #
                # 只静音不行 —— rc=0 加空输出等于「跑成了但没数据」，
                # 比报错更难发现（退出码还是 0）。必须给**合法** JSON。
                #
                # N438（第一百四十三轮）：M106 要给这道分支置
                # _BLOCKED_BY_GUARD（否则退出码是 0 = 成功），而它在
                # _main 里是**局部名** —— global 声明必须在**任何**赋值
                # 之前（Python 的 "name is assigned to before global
                # declaration" 是 SyntaxError，不是运行时错）。
                global _BLOCKED_BY_GUARD
                #
                # M105（A105）：hint 写死 "再跑 --status"，而这道分支是
                # **所有**命令共用的 —— 推送路径也走这里。
                # 实测：`push_api.py --json -m x --yes`（无基线）拿到的
                # hint 是「先跑 --init-baseline，再跑 --status」，
                # 照它做解决不了推送（--status 也是查询，不建基线）。
                # 与 M80/M86 同族：那段是照 --status 写的，push 复用了它。
                # 空格放在取值里：查询类要 "再跑 --status"（与原文案一致），
                # 其余是 "再跑本命令"（"再跑 本命令" 反而别扭）。
                _what = (" --status" if (opts.get("status") or opts.get("scope")
                                         or opts.get("mine"))
                         else "本命令")
                if opts.get("json"):
                    print(json.dumps({
                        "ok": False,
                        "baseline": False,
                        "reason": "no_baseline",
                        "hint": f"先跑 --init-baseline 建立基线，再跑{_what}",
                    }, ensure_ascii=False))
                    _mark_json_emitted()
                    _BLOCKED_BY_GUARD = "no_baseline"
                    return
                print("\n⚠️  没有基线记录（首次使用），无法判断远端文件是否被他人改动过。")
                print("   请先跑一次 `python3 push_api.py --init-baseline` 建立基线，再推送。")
                #
                # M106（A105，P2）：上面两行说完就 `return`，退出码是 **0**。
                # 而 0 = 成功 —— 实测：
                #   $ push_api.py -m "no baseline" --yes
                #   退出码=0、远端零变化、状态文件根本没建（只有 .lock）
                #
                # CI 是**必然命中**的：状态文件在 `<ROOT>/.git/push-sync.json`，
                # 而 .git/ 的内容不入库 —— 全新克隆一次基线都没有。
                # 于是"CI 推送成功"而远端一个字节没变，且没有任何非零信号。
                #
                # 这正是 M80/M86 那两处点名要消灭的形态（"rc=0 加空输出
                # 等于跑成了但没数据，比报错更难发现"），只是它们修的是
                # **JSON 输出**，退出码这条一直没人碰。
                #
                # 语义上它属于「被防护拦下」：什么都没推、改动也没丢，
                # 按提示跑 --init-baseline 即可 → 契约给的是 **2**。
                # 置 _BLOCKED_BY_GUARD：_main 的 finally 会「先取值 →
                # 再复位 → 最后写回」到 _LAST_RUN_BLOCKED（M42 那套三步），
                # 顶层据此 sys.exit(2)。这里不能写成 return 2。
                _BLOCKED_BY_GUARD = "no_baseline"
                return
            init_baseline(head_sha, rstate)
            return
        if not reset:
            # 旧基线是无条件覆盖的话，第一层文件级防护会随之失效，必须拦一道。
            #
            # M86：这条与 M80 修的那条**相隔 7 行**、性质完全相同
            #（都是早退、都是 rc=0、都是纯文本），而 M80 只修了紧邻那一处。
            # 只静音不行 —— rc=0 加空输出等于「跑成了但没数据」，
            # 必须给合法 JSON（M80 的原话）。
            if opts.get("json"):
                print(json.dumps({
                    "ok": False, "baseline": True,
                    "reason": "baseline_exists",
                    "action": "init_baseline",
                    "base_commit": str(state.get("base_commit")),
                    "files": len(state.get("files") or {}),
                    "hint": "加 --reset-baseline 重设（会丢弃现有记录，"
                            "文件级防覆盖校验将暂时失效）",
                }, ensure_ascii=False))
                _mark_json_emitted()
                return
            print(f"\n⚠️  基线已存在（记录于 commit {str(state.get('base_commit'))[:8]}），未做任何改动。")
            print("   确要丢弃旧基线并重设，请加 `--reset-baseline`：")
            print("   重设后「远端文件是否被他人改过」将失去参照，请确认远端已是最新。")
            return
        ans = "y" if yes else safe_input(
            f"\n重设基线会丢弃现有 {len(state.get('files', {}))} 条记录，"
            f"文件级防覆盖校验将暂时失效。继续？(y/N) "
        ).strip().lower()
        if ans != "y":
            _cancelled()
            return
        init_baseline(head_sha, rstate)
        return

    # -------- 基线归属校验 --------
    # 换仓库推时旧基线仍生效，会让所有文件报「可能被他人改动」——
    # 方向虽然是 fail-closed，但把人引向错误的排查方向。
    #
    # N299：state 为 None 时（基线缺失 + 纯本地命令）这段**必须跳过**。
    # 原先跳过了闸门却仍然走到这里，实测报
    #     AttributeError: 'NoneType' object has no attribute 'get'
    # —— 跳过闸门只是第一步，后面所有 `state.xxx` 都得一起处理。
    # 这两个命令根本不读 state 的这些字段。
    if state is not None and state.get("remote") not in (None, f"{OWNER}/{REPO}"):
        raise SystemExit(
            f"基线属于另一个仓库：{state.get('remote')}，当前目标是 {OWNER}/{REPO}。\n"
            f"  不是他人改动，是仓库换了。请换用对应的 STATE_PATH，或用 --reset-baseline 重建。"
        )
    # branch 也一起校验：原来只在 state 里存了却从不比对，改 BRANCH 常量重跑
    # 会让 A 分支的基线拿去校验 B 分支，所有文件都可能被误判。
    if state is not None and state.get("branch") not in (None, BRANCH):
        raise SystemExit(
            f"基线记录的是分支 {state.get('branch')}，当前目标是 {BRANCH}。\n"
            f"  跨分支复用基线会让文件级校验全部失真。请换 STATE_PATH 或 --reset-baseline。"
        )

    # -------- 基线版本迁移 --------
    # lmap 用 callable 惰性传入：本文件后文虽然也算过 local_state_map()，
    # 但那在迁移点**之后**（且只在有冲突时才算），这里取不到。
    # 惰性是为了「不需要迁移时」不白跑一次 git（大仓库上一次调用可不便宜）。
    # N299：state 为 None（基线缺失 + 纯本地命令）时，迁移与 setdefault
    # 同样无意义且会崩 —— 迁移是**把旧基线转成新格式**，没有基线可转。
    # 与上面两处同族：跳过闸门只是第一步，后面每段 `state.*` 都要处理。
    if state is not None:
        state, migrated = migrate_state(state, rstate, lmap=local_state_map)
        if migrated:
            save_state(state)
            _seen = state.get("local_seen")
            # M81 ③：同上。只静音、不补 JSON —— 这条是**过程提示**，
            # 后面还有正常的结构化输出；静音后消费者拿到的是完整的 JSON。
            #
            # N386（第一百二十四轮，中危）：判据改 `_stream_active()`。
            # 实测 `--scope --mine` 的输出第二行就是本句 —— 它与
            # 4323 那句一起，把 v1→v2 升级后的第一次 `--mine` 变成
            # 「管道里前两行是文件名」。升级后第一次跑必中，
            # 因为老基线没有 mode 字段**必然**触发迁移。
            if not _stream_active():
                print(f"  · 基线已迁移到 v{STATE_VERSION}"
                  f"（补记 mode 取自远端当前值"
                  + (f"，补齐 local_seen {len(_seen)} 条以支持删除检测）"
                     if _seen is not None else "）"))
        state.setdefault("tasks", {})
        state.setdefault("conflicts", [])

    # -------- 工作流判定 --------
    # 不能写成 `state.get("workflow") or DEFAULT_WORKFLOW` 就完事：
    # 下面 PR 分支的判据是精确匹配 wf == "pr"，而 direct 是「else」——
    # 任何非 "pr" 的值（"PR" 大写、拼错、将来新增取值）都会**静默落到
    # direct**，即绕过 PR 流程直推主干（实测：workflow="PR" 时无分支、
    # 无 PR，改动直接上主干）。退化方向恰好是最危险的一侧，且用户全程无感知。
    #
    # 所以未知取值要**报错**而不是取默认值 —— 与 _local_head_rewound 里
    # 「git merge-base 退出码异常时 fail-closed」是同一个取舍：
    # 判不了就停下来问，不要替用户选一条更危险的路。
    # N299 同族：state 为 None 时取默认值即可（纯本地命令不看 workflow）。
    raw_wf = (state.get("workflow") if state else None) or DEFAULT_WORKFLOW
    wf = "direct" if opts["direct"] else str(raw_wf).strip().lower()
    if wf not in ("pr", "direct"):
        raise SystemExit(
            f"基线里的 workflow 是 {raw_wf!r}，只接受 'pr' / 'direct'。\n"
            f"  未知取值不会静默回退（回退到 direct 等于绕过 PR 流程直推主干）。\n"
            f"  手工改 {STATE_PATH} 里的 workflow 字段，或跑 --reset-baseline 重建。")
    # --direct 必须落盘：否则用户每次都得手动加参数，更糟的是
    # --reset-baseline 之后（旧版把 workflow 写死默认值）会被静默改回 PR
    # 工作流 —— 下次推送走的是另一套逻辑且毫无提示。
    # M64（A72）：无基线 + `--direct` + 纯本地命令 → AttributeError 崩溃。
    #
    # 报告实测：`--audit --direct` 退出码 1，
    #   AttributeError: 'NoneType' object has no attribute 'get'
    #
    # 成因是 N299（"纯本地命令不需要基线"）豁免了闸门后，state 可能为 None，
    # 而它处理了 4 处 state.* 引用、**漏了这一处**。它自己的注释把规律
    # 写对了（"跳过闸门只是第一步，后面所有 state.xxx 都得一起处理"），
    # 也把症状写对了，偏偏自己正是漏的那一处。
    #
    # 注意上面 L10783 已经用了正确形态 `(state.get(...) if state else None)`，
    # 相距十行却是两种写法 —— 正是这类"同一规律只落实了一部分"的痕迹。
    #
    # 无基线时没什么可落盘的（save_state 需要一份 state），
    # 且纯本地命令（--audit / --clean-conflicts）本就与 workflow 无关，
    # 所以安全地跳过即可 —— 不做"为了落盘而临时造一份 state"这种事，
    # 那会在用户没要求时凭空产出一份基线文件。
    # 开了分支保护就别直推了：服务端会拒，而 403 的报错看不懂。
    # 放在任何远端写操作之前 —— 此时还没有建分支、没有 PATCH。
    #
    # M114（A108，复核 N440）：这道闸门拦的是**直推主干**这一个动作，
    # 而它原先放在所有子命令分派**之前**，于是连根本不写远端的命令一起拦。
    # 实测（workflow=direct + 已开保护，9 个子命令全部 exit 2）：
    #   --status / --scope / --mine / --audit / --clean-conflicts / --prune
    #   / --dry-run / --resolve / --mark-synced
    # 其中 --audit 是脚本自述「排查上周那次到底推上去没有只能靠它」的
    # 唯一本地痕迹，--clean-conflicts 负责清理装着远端明文的副产物 ——
    # 恰恰是"需要排查"时才想到要跑的两个，此时都不可用。
    #
    # 收窄到推送路径，复用上面现成的 `_is_push` 判据（同一份名单，
    # 不新造概念）。
    # `_is_push` 那份名单是给 N59（空远端树）用的，里面没有
    # --scope / --mine / --dry-run —— 而这三个同样不写远端：
    # 前两个是查询，第三个是预演（脚本自己的措辞是"未做任何推送"）。
    # 实测：只加 `_is_push` 时它们仍被拦（--dry-run 尤其说不通：
    # 预演被服务端配置拦下）。所以这里再叠一层，名单各自独立，
    # 不去动 N59 那份（改它会影响另一道判据的覆盖面）。
    if (wf == "direct" and _is_push
            and not any(opts.get(k) for k in ("scope", "mine", "dry"))):
        _refuse_direct_if_protected(BRANCH, opts.get("direct"))
    #
    # M114（A108，复核 N440）：落盘原先排在保护检查**之前** ——
    # 于是"第一次跑 --direct 被拦下"时 workflow 已经写进状态文件了，
    # 而它**没有任何 CLI 能切回 pr**（无 --pr / --workflow 选项）。
    # 实测：全新状态跑一次 `--direct`（当场被拦，退出码 2），
    #   落盘后 workflow == 'direct'  → 此后**所有**命令永久 exit 2。
    # 用户第一次看到的只是"这次不能直推"，并不知道已经把后续全锁了。
    #
    # 挪到检查之后：会被拦下时就不写（拦下会抛 SystemExit，走不到这里），
    # 锁不成立。wf 仍按命令行判断（此时状态文件里还没写），与上面一致。
    if opts["direct"] and state is not None and state.get("workflow") != "direct":
        state["workflow"] = "direct"
        save_state(state)

    # -------- PR 子命令 --------
    if opts["prune"]:
        # N73：--prune --json 此前被静默忽略（prune 没有 as_json 参数，
        # 调用点也不传）。注释明确声明「只在 --status / --prune 上生效」，
        # 于是用户以为拿到了可解析输出，实际是文本 —— json.loads 会失败。
        # 而 show_status 的 docstring 专门讲过「半结构化输出比没有更危险，
        # 人会拿它当可靠依据」。声称支持却静默降级正属于那类危险。
        if opts.get("json"):
            # M80-2（A84）：原先这里只 print、**没置位 _DISPATCH_REACHED**
            #（M82 补 audit / clean_conflicts 时以「它自己 print 了一句」
            # 为由跳过了本分支）。改用 `_warn_json_unsupported()`：
            # 置位 + 打印 + 让 finally 不再打第二句，三件事一起做。
            _warn_json_unsupported(
                "  ! --prune 暂不支持 --json，按文本输出。"
                "（结构化输出目前只在 --status / --scope 上可用）")
        prune(state, grace_days=opts["stale_days"])
        return
    if opts.get("clean_conflicts"):
        # N132：N106 在函数体里加了 `--yes` 通路（检查工作区冲突标记），
        # 但这里是**唯一调用点且不传 yes** —— 那段代码永不可达，
        # --yes 依旧被静默忽略。
        # 同一分派区里 delete_branch_cmd / close_pr / do_merge 都传了 yes。
        #
        # M82（A103，dogfood 实测）：`_mark_dispatch_reached()` 只在
        # --status / --scope / --mine 三个分支调用，这里漏了。于是
        # `--clean-conflicts --json` 是**静默降级**：纯文本、退出码 0、
        # 连「不支持」的提示都没有 —— 因为 finally 那句要看
        # _DISPATCH_REACHED 才打。
        # 对照 --prune：它自己 print 了一句，所以一直有提示。
        #
        # 实测：--audit --json / --clean-conflicts --json 两者都是
        # rc=0 + 纯文本 + 零提示，json.loads 直接失败。
        _mark_dispatch_reached()
        clean_conflict_artifacts(state, yes=yes)
        return
    if opts.get("audit"):
        # M82：同上。--audit 本来就只按文本输出，但**必须**告诉 --json
        # 的使用者这一句不是 JSON —— 否则 CI 读到 rc=0 会当成
        # "跑成了但没数据"，比报错更难发现。
        _mark_dispatch_reached()
        show_audit(limit=opts.get("audit_limit") or 20)
        return
    if opts["delete_branch"]:
        # N530（第一百七十四轮，中危）：本分支此前既不置位
        # _DISPATCH_REACHED、也不调 _warn_json_unsupported ——
        # 于是 `--delete-branch x --json` 是 rc=0 + 纯文本 + 零提示，
        # 与 M82 已判定为缺陷的 --audit / --clean-conflicts 同形。
        # AI / CI 拿到 rc=0 会理解成「跑成了但没数据」。
        _mark_dispatch_reached()
        delete_branch_cmd(state, opts["delete_branch"], yes=yes)
        return
    if opts["status"]:
        _mark_dispatch_reached()
        show_status(state, head_sha, as_json=opts.get("json", False))
        return
    if opts["scope"]:
        # --scope 与 --status 同构：远端树由 show_scope **内部**自取，
        # 不复用分派前那次 remote_state_map —— 那条路径下方有无条件
        # print（M86），会落在 JSON 之前让输出不可解析。
        _mark_dispatch_reached()
        show_scope(state, head_sha, as_json=opts.get("json", False),
                   only_mine=opts.get("mine", False))
        return
    if opts.get("mine"):
        # --mine 单独使用也认：它与 --scope 同源，不另起判据。
        # 少打一个 --scope 不该变成"未知参数"（N102 的同族要求）。
        _mark_dispatch_reached()
        show_scope(state, head_sha, only_mine=True)
        return
    if opts["close_pr"]:
        _mark_dispatch_reached()  # N530
        b = opts["branch"] or state.get("task_branch")
        if not b:
            raise SystemExit("--close-pr 需要 --branch <名字>，或当前已有任务分支")
        close_pr(state, b, yes=yes)
        return
    if opts["merge"]:
        _mark_dispatch_reached()  # N530
        n = None
        if explicit and explicit[0].isdigit():
            n = int(explicit[0])
        elif opts.get("branch"):
            # N69：--merge 此前**完全忽略** --branch，静默合并
            # state["pr_number"]（那是**最后**创建的那个 PR）。
            # 而相邻的 --close-pr 明确认 --branch ——
            # 同一个参数在两个相邻子命令里语义相反，用户无从预期。
            #
            # --hold + --new-task 攒出多个任务分支时，用户想合并早期分支：
            #   python3 push_api.py --merge --branch task/old
            # 实际合并的却是 task/new 的 PR —— 而合并进主干不可逆。
            _want = opts["branch"]
            _all = _paginate("/pulls?state=open")
            _hit = [pr for pr in _all
                    if (pr.get("head") or {}).get("ref") == _want]
            if _hit:
                n = _hit[0].get("number")
                print(f"  · 按 --branch {_want} 定位到 PR #{n}")
            else:
                raise SystemExit(
                    f"--branch {_want} 没有对应的开放 PR。\n"
                    f"   --merge 需要 PR 号或分支名；当前记录的 PR 是 "
                    f"{state.get('pr_number')}。\n"
                    f"   列出全部开放 PR：python3 push_api.py --prune")
        elif state.get("pr_number"):
            n = state["pr_number"]
        if n is None:
            # 开了多个并行 PR 时，合并完当前那个后 pr_number 就空了，
            # 第二次 --merge 会直接报错 —— 得把剩下的列出来让人挑，
            # 否则用户只能去网页上翻 PR 号。
            open_prs = _paginate("/pulls?state=open")
            # N90：原先按 task/ 前缀过滤 —— 正是 prune() 已修掉的那类
            # "只认一种写法"（prune 的 docstring 原话：`pr/ fix/ feature/`
            # 等前缀的分支**静默漏报**，而 --prune 的全部价值就在不漏）。
            # 这里同样：用户用 -b pr/xxx 建的分支，其 PR 会被过滤掉，
            # 于是报「当前没有开启中的任务 PR」—— 明明有，却说没有。
            # 改为不过滤前缀，只排除主干自身。
            open_prs = [p for p in open_prs
                        if (p.get("head") or {}).get("ref") != BRANCH]
            if not open_prs:
                raise SystemExit(
                    "--merge 需要 PR 号（例：--merge 42）；"
                    "当前没有开启中的 PR")
            if len(open_prs) == 1:
                n = open_prs[0]["number"]
            else:
                print("\n开启中的任务 PR：")
                for p in open_prs:
                    print(f"  #{p['number']}  {p['head']['ref']}   {p.get('title') or ''}")
                raw = safe_input("输入要合并的 PR 号：",
                                kind="entry").strip()
                if not raw.isdigit():
                    # N311（第九十三轮，中危）：K1 之后非 TTY 恒返回 ""，
                    # 于是 `isdigit()` 恒 False → 恒取消，**且退出码 0**。
                    # CI 判定"--merge 执行成功"，实际零 PR 被合并 ——
                    # 与 N240（被防护拦下返回 0）同一类"静默成功"。
                    #
                    # 三个问题叠在一起：
                    #  ① 出口不存在：--yes 救不了它（这处不是确认，是数据录入）
                    #  ② 脚本知道答案却不给：它刚把候选列表完整打印出来，
                    #     却不说"改用 --merge 42"—— 而无 PR 时的报错
                    #     （见上方）恰恰给了示例，有 2 个 PR 时反而不给
                    #  ③ 退出码 0
                    #
                    # 另外「所有确认按否处理」这句文案对不上场景：
                    # 这里不是确认，是数据录入，"否"对 PR 号没有意义。
                    _cands = [str(x.get("number")) for x in (open_prs or [])
                              if x.get("number")][:3]
                    _eg = _cands[0] if _cands else "42"
                    print("\n❌ 非交互环境无法选择 PR（无 TTY / 管道 / CI）。")
                    print(f"   请显式指定：python3 push_api.py --merge {_eg}")
                    if len(_cands) > 1:
                        print("   候选：" + "、".join(_cands))
                    raise SystemExit(2)
                n = int(raw)
        do_merge(state, int(n), opts["method"], yes=yes,
                 allow_stale=opts["allow_stale"])
        return

    # -------- 标记已同步（解除过期拦截）--------
    if opts["mark_synced"]:
        _mark_dispatch_reached()  # N530
        mark_synced(state, head_sha, rstate)
        return

    # -------- 拉取：三方合并远端改动到本地 --------
    # 必须紧接在 mark_synced 之后、P0-1 之前：合完本地就包含了远端最新，
    # 后面的过期校验才有放行依据。
    if opts["pull"]:
        _mark_dispatch_reached()  # N530
        # force **不传**给 pull：它在这里会被当成「忽略未解决冲突」，
        # 那是完全不同的语义（见 pull 内注释）。忽略冲突必须显式 --ignore-pending。
        #
        # N367（第一百一十五轮，中危）：pull() 在有未解决冲突/拉取失败时
        # 返回 **False**，而这里**没有接收返回值** —— `return` 直接结束
        # main()，顶层无 sys.exit → 退出码 0。
        #
        # 决定性对照（同一仓库、同一份未解决冲突）：
        #     --pull                     → exit=0   ★
        #     同一份状态跑默认推送        → exit=3   （契约：3 = 冲突未解决）
        #
        # **同一个"有未解决冲突"的事实，两个命令给出 0 和 3。**
        #
        # 而 --pull 是唯一**制造**冲突状态的命令 —— 它自己却用"成功"
        # 报告这件事。CI 的典型序列是 `--pull` → 处理 → 推送：
        #   · --pull 返回 0 → CI 判定同步完成，继续往下走
        #   · 冲突文件一个都没合进来，工作区躺着 .remote / .base
        #   · 直到推送那一步才拿 3，而此时 CI 已执行了若干基于"已同步"
        #     假设的步骤
        #
        # 与 N361（拦下出口用 1）、N362（取消用 0）完全同族：
        # 那两条已修，**这条是同一张表上剩下的最后一个洞**。
        #
        # 只把"冲突"映射成 3；pull 返回的 False 也可能是"部分文件拉取
        # 失败"（那些**不是**冲突，重跑即可），那属于运行错误 → 1。
        _ok = pull(state, head_sha, rstate, only=explicit or None,
                   ignore_pending=opts.get("ignore_pending", False))
        if not _ok:
            # 用 pull 写回的 state 判 conflicted（与推送路径同一份判据），
            # 不另起一套 —— 两套判据迟早给出不同结论（N284/N306 的教训）。
            if (state or {}).get("conflicts"):
                _blocked("存在未解决冲突（--pull 未合并），"
                         "逐个 --resolve 后再推送", code=3)
            # False 也可能来自"部分文件拉取失败" —— 那些**不是**冲突
            # （pull 自己就写明"不需要 --resolve"），属运行错误 → 1。
            #
            # N570（第一百八十六轮，低危）：但 False **也**来自
            # `_refuse_if_rewound` —— 那时它已经置了 `_BLOCKED_BY_GUARD`
            # （"rewound_local_head"），是**被防护拦下**，契约退出码是 2。
            # 而这里 `raise SystemExit(1)` 当场结束进程，顶层那个
            # `if _LAST_RUN_BLOCKED: sys.exit(2)` 根本轮不到执行 ——
            # 实测 --pull 被回退判据拦下时退出码是 1，而推送路径同样是
            # 被它拦下却是 2。CI 拿到 1 会当成"命令失败"，与"部分文件
            # 拉取失败"这种真运行错误混在一起，分不出"需要人处理"。
            if _BLOCKED_BY_GUARD:
                raise SystemExit(2)
            raise SystemExit(1)
        return

    # -------- 冲突解决 --------
    if opts["resolve"]:
        _mark_dispatch_reached()  # N530
        if not explicit:
            raise SystemExit("--resolve 需要至少一个文件路径")
        for f in explicit:
            rel = safe_rel(f)
            if rel is None:
                print(f"  ! 忽略越界路径：{f}")
                continue
            resolve(state, rel, head_sha, rstate,
                    yes=bool(opts.get("yes")))
        return

    # -------- Guard 管线：推送前的三道硬闸 --------
    # 顺序不再依赖「谁写在前面」，而由 GUARDS 列表 + 每个 Guard 的 after
    # 声明决定，并由 validate_guard_order() 校验（见文件顶部 Guard 段）。
    #
    # N250：那句「由 validate_guard_order() 校验」是**空头支票** ——
    # 该函数全文件零调用点，只在测试里被直接调。于是：
    #   · 新增 Guard 忘了声明 after → 没人报
    #   · 有人手滑调换 GUARDS 顺序 → 没人报
    # 而 Guard 顺序是防护的**语义**（先判冲突再判过期，顺序错了
    # 会给错的提示），静默错序正是最难发现的一类。
    #
    # 接在管线入口：每次推送前校验一次。
    _order_problems = validate_guard_order()
    if _order_problems:
        print("\n  ! Guard 顺序声明有问题（防护可能按错误的先后执行）：")
        for _gid, _why in _order_problems:
            print(f"      - {_gid}: {_why}")
        print("     这是脚本自身的配置问题，请反馈给维护者。")
    #
    # 细粒度放行清单与全局开关只是 Guard 的**输入**，与任何判据无关。
    # 原来它们夹在「未解决冲突拦截」与「第零层」之间，那是一种隐式耦合：
    # Guard 不该依赖自己被夹在哪两段代码中间。
    allowed = set()
    for f in opts["force_files"]:
        rel = safe_rel(f)
        if rel is None:
            raise SystemExit(f"--force-file 路径越出仓库范围：{f}")
        allowed.add(rel)

    # P0-1 只由**全局** --force-overwrite 放行，--force-file 不行。
    #
    # 曾经这里写成 `force or bool(allowed)`，理由是「点名即承担风险」。
    # 但 P0-1 是全局闸门而非按文件判定：它回答的是「你的本地副本是不是
    # 从最新主干改的」。用户点名 e1.txt 时，并不知道自己的副本已过期，
    # 也就无从同意「把 e2 的他人改动回退掉」—— 这个同意是无效的。
    #
    # 实测后果：--force-file e1.txt 会让本地旧版的 e2 一并进 diff，
    # GitHub 三方合并时 theirs==base、ours 变了 → 直接采纳 ours，
    # 他人对 e2 的改动被静默回退且合并成功、零报错。
    ctx = PushCtx(state=state, opts=opts, head_sha=head_sha, wf=wf,
                  force=force, allowed=allowed, explicit=explicit,
                  rstate=rstate, lmap=None, dry=dry,
                  todo=[], blocked=[], risky=[])
    # files 此刻**尚未最终确定**（后面还要并入 extra），只在前置 Guard 用不到它，
    # 由 FileBaselineGuard 调用前再赋给 ctx。

    blocked_verdict = run_guards(ctx)
    if blocked_verdict is not None:
        _enforce(blocked_verdict)
        return
    # 不再回写 synced / stale_local / bypass：pyflakes 确认它们后续无人读，
    # 留着只会制造「改了局部变量以为生效」的假象（改 Guard 里那份才有用）。

    lmap = local_state_map()

    # -------- 远端残留：待删除清单 --------
    # 必须在**任何提前返回之前**：没有待推送改动时脚本会直接 return，
    # 放在推送分支里的话，本地删了文件却无其他改动 → 完全不提示 →
    # 远端永久残留，而使用者（尤其 AI）以为删干净了。
    try:
        # N306：改用 A∪B 并集 —— 与执行链同源。
        # 原先只用 A（pending_deletions），而执行链的 _pending_dels
        # 来自 B（_detect_deletions）。于是「rm a.txt 未 git rm」时
        # 会真的删，却不提示 —— 正是上方注释定为存在理由的那个场景。
        _pd = pending_deletes_union(state, lmap, rstate)
        if _pd:
            # 只**记录**，打印交给运行出口（_main_wrapped 的 finally）。
            #
            # 这里原本直接打印，但它位于推送流程中段 —— 后面还有几十行
            # blob 进度 / 分支 / PR 链接，清单被冲到屏幕外。
            # 而"再在收尾打一遍"会让同一份清单出现两次，
            # 正是这批报告反复批评的"同一份逻辑两处、改一漏一"。
            #
            # 所以收敛：**一次运行只打印一次，且在最后**。
            # 出口能覆盖所有 return（含"没有待推送改动"的提前返回），
            # 位置又必然在末尾 —— 两全。
            report_pending_deletes(_pd, emit=False)
            state["pending_deletes"] = _pd
        elif state.get("pending_deletes"):
            # 已处理完：远端确认没了就摘掉，不再反复提示
            _still = [p for p in state["pending_deletes"]
                      if p in (rstate or {})]
            if _still:
                state["pending_deletes"] = _still
                # N177：同一个 if/elif 的两个分支用了不同 emit 语义 ——
                # if 分支 emit=False（登记），elif 这里却用默认的
                # emit=None（立即打印）。于是"已处理完但远端仍在"这一类
                # 清单被打回流程中段，后面跟着几十行 blob 进度 ——
                # 正是 #39 明确要消除的形态。
                report_pending_deletes(_still, emit=False)
            else:
                state.pop("pending_deletes", None)
    except Exception as e:
        print(f"  ! 待删除检测失败（不影响推送）：{type(e).__name__}: {e}")

    # -------- 搬家（重命名）提示 --------
    # 必须在推送**之前**：推完才说的话，用户看到 ✅ 就已经走人了。
    #
    # 同时把「已搬走」的对应关系**记进 state**：
    # 事后推断不可靠 —— 推完这次新路径也进了基线，再跑 detect_moves
    # 时「新增路径」已为空，配不出对，pull 就会把旧路径又拉回来
    # （实测复现）。搬家是一次性事件，要在发生的当下记下来。
    try:
        # N372：判据 3（local_seen）已在 detect_moves 内部生效 —— 远端独有
        # 文件不再被判成「搬家」，pull 不再据此永久跳过它（数据缺失）。
        #
        # will_push 那一半**没有**在这里接上：本块位于 P0-1 之后、
        # 但最终待推送清单 `todo` 要等 FileBaselineGuard 跑完才确定
        #（实测在此处引用 `todo` 是 UnboundLocalError —— 它属于 Guard
        # 的方法作用域，不在 _main 里）。硬传一个"此刻的候选集"会与
        # 执行侧不同源，反而制造新的不一致；留给后续条目把本块挪到
        # todo 确定之后（那时才能与执行侧真正同源）。
        _mv, _gh = detect_moves(state, lmap)
        # N457：传 state / lmap，让这里的计数与出口清单同源。
        # M161：把点名范围传给 report_moves —— 否则它会在「点名只推新路径」
        # 时仍断言「旧路径将在本次推送中删除」，与执行侧的裁剪相反。
        # 口径与执行侧点名集合的**构造**同源（同为 safe_rel(explicit) 的非空
        # 结果；空集合转 None，不传 = 不过滤，行为与修复前一致）。
        # 执行侧之后还会再减去被消毒拦下的路径（rejected / dropped 等），
        # 那些路径本就不在 moves 里，对本句的判定没有影响。
        report_moves(_mv, _gh, rstate, state=state, lmap=lmap,
                     allowed=({_r for _r in (safe_rel(_f) for _f in explicit)
                               if _r} if explicit else None))
        # ⚠️ 必须先建 `_ma` 再把 _mv 并进去，且**无条件**执行：
        # 原写法 `_ma = state.get("moved_away") or {}` 在存量为空时拿到的是
        # 一个**新**字典，for 循环写进的是它、而 state 里那份仍是 None。
        # 旧代码靠最后的 `state["moved_away"] = {…_ma…}` 把结果写回去才兜住；
        # 改成就地过滤 state 之后，这一步就丢了 —— 实测本次检测到的搬家
        # 一次都没写进 state（moved_away 恒为 {}）。
        _ma = dict(state.get("moved_away") or {})
        if _mv:
            for _o, _n in _mv:
                _ma[_o] = _n
            # 新路径也被删了就不记：内容没着落，不能跳过拉取
            # N373：补「记录是否仍然成立」—— 远端旧路径已换成别的内容
            # 时失效。原判据只看新路径在不在，记录只增不减（与 N374 同族）。
            #
            # N474：这段清理原先挂在 `if _mv:` **内部**，于是只有本次
            # 检测到新搬家时才过滤一遍。而 `_mv` 为空才是常态——推完
            # 那次新路径就进了基线，detect_moves 的 added 集合此后恒空。
            # 时序上必然的组合：搬家当下推送（记录有效）→ 远端旧路径
            # 内容变了（记录失效）→ 此后每次 _mv 都空 → 失效记录永久
            # 留在 state 里，--scope 侧持续输出错误 tag。
            # 移出 if，无条件执行。
        state["moved_away"] = _prune_moved_away(_ma, lmap, rstate,
                                      state.get("files"))
    except Exception as e:
        # 提示失败绝不能影响主流程：它只是告知，不参与任何判据。
        # 但也不能静默吞掉 —— 那会让提示「看起来没坏其实坏了」。
        print(f"  ! 搬家检测失败（不影响推送）：{type(e).__name__}: {e}")

    # -------- 任务分支（PR 工作流）--------
    # 放在 P0-1 之后：P0-1 会拦下「本地副本落后」，被拦时不该留下空分支。
    # 每次推送都**新建**分支，基点 = 当前主干最新 —— 基点越新，PR diff 越干净，
    # 合并时三方合并的冲突面越小。--hold 是唯一的例外（攒改动，显式复用）。
    # git 通道默认关闭，只在送出段按探测结果启用。
    # 必须在这里初始化：PR 新分支 / --hold 复用分支 / direct 三条路径
    # 只有前一条会给它赋值，其余两条不初始化就是 NameError
    #（而 NameError 发生在**建对象之后**，正是最容易留下僵尸分支的时刻）。
    _use_git = False
    target_ref, base_sha = BRANCH, head_sha
    if wf == "direct":
        # direct 一样可以走 git：git_send push 到主干 ref，
        # 语义与 API 的 PATCH refs/heads/main 一致。
        _use_git = _git_channel_ok()
        if _use_git:
            print(f"\n  · 走 git 通道直推 {BRANCH}（基点 = {head_sha[:8]}）")
    branch_missing = False          # 该分支在远端还不存在，需要创建
    # M122（A113，P3）：N380 的出路要按分支来源分流。
    # 只有来自 --hold 复用时 --new-task 才是有效的出路；显式 --branch
    # 下它根本不起作用（见下方），照旧建议等于让用户照抄一个无效命令。
    _branch_from_hold = False
    if wf == "pr":
        branch = None
        if opts["branch"]:
            branch = opts["branch"]
            # 用主干当任务分支必须硬拦：那等于绕过 PR 流程直推主干，
            # 全部覆盖防护（第一层文件级校验在 PR 模式下是「提示」而非
            # 「拦截」）形同虚设；且 PATCH 主干失败后没有回滚路径。
            if branch == BRANCH:
                raise SystemExit(
                    f"不能用主干 {BRANCH} 当任务分支。\n"
                    f"  直推主干会绕过 PR 流程与全部覆盖防护，且失败后无法回滚。\n"
                    f"  不指定 --branch 时脚本会自动生成 {TASK_PREFIX}* 分支。")
            if opts["new_task"]:
                # M122（A113，P3）：--new-task 只在下面 --hold 那个 elif 里
                # 被检查，而显式 --branch **优先命中**，根本走不到 ——
                # 于是它被静默忽略。而 N380 在复用分支时恰恰建议
                # 「要合成一个请新建分支（--new-task）」，实测用户命令行里
                # 已经带了它：分支照旧复用、建议原样给出。与 M77
                # （零成功却报成功数）、A95（提示承诺脚本不会做的事）同族。
                # 必须说清，不能静默：静默等于承认一个用户不知道的忽略。
                print(f"\n  ! --new-task 只对 **--hold 复用分支**生效；"
                      f"本次显式指定了 --branch {branch}，该选项**被忽略**。")
                print("     要另起一个分支：去掉 --branch（脚本会自动新建），"
                      "或换一个远端不存在的分支名。")
        elif opts["hold"] and state.get("task_branch"):
            if opts["new_task"]:
                # branch 保持 None → 下面新建分支。
                # 语义：放弃当前任务分支、另起一个。默认已是「每次新建」，
                # 所以它只在 --hold 攒改动时有区别 —— 那时默认会复用旧分支。
                print(f"  · --new-task：放弃当前任务分支 "
                      f"{state.get('task_branch')}，另起一个新分支")
            else:
                branch = state["task_branch"]
                _branch_from_hold = True

        if branch is None:
            # 这一阶段只**取名**、不创建：下面若发现没有待推文件就会直接 return，
            # 那时建分支只会往远端丢一个空分支。创建推迟到确认有东西要推之后。
            for _ in range(5):
                branch = new_branch_name()
                if ref_sha(branch) is None:      # 撞名就换一个（GET，无副作用）
                    break
            else:
                raise SystemExit("连续 5 次生成的分支名都已存在，请检查远端是否有异常残留")
            branch_missing = True
            bhead = head_sha                     # 新分支的内容 = 当前主干
        else:
            bhead = ref_sha(branch)
            if bhead is None:
                branch_missing = True
                bhead = head_sha
            else:
                base_rec = (state.get("tasks") or {}).get(branch, {}).get("base_commit")
                if base_rec and base_rec != head_sha:
                    print(f"\n  ! 分支 {branch} 建在 {base_rec[:8]}，主干已到 {head_sha[:8]}")
                    # A4-H1：旧文案承诺「本次会把主干的更新一并同步进分支」，
                    # 而主干更新恰恰落进 skipped_stale（本地 == 基线、只是分支旧），
                    # 不进 extra —— 承诺从未兑现。
                    #
                    # **不要**照 A4 的建议把 skipped_stale 也塞进 extra：
                    # 实测三方合并下，分支保持旧版本时 theirs==base，合并直接采纳
                    # ours（主干），结果正确且无冲突（真实 git 仓库验证：主干 v2
                    # 在合并后仍是 v2）。一旦把主干版本写进分支，主干**再次**前进时
                    # theirs != base != ours，干净的合并反而变成冲突。
                    # 所以修的是这句文案，不是判据。
                    print("    你没改过的文件保持分支原有版本（不塞进分支）；"
                          "合并时主干优先，不会出现「回退主干」")
                    print("    本地要跟进主干请跑 --pull（本次不会替你合）")
        target_ref, base_sha = branch, bhead
        # N194：--hold 复用已有分支时 _use_git 保持 False，
        # 于是复用分支这条路径**永远走 API**，git 通道形同半残。
        # 分支已存在与否不影响 git 能否 push —— 语义相同。
        _use_git = _git_channel_ok()
        if _use_git:
            print(f"\n  · 走 git 通道推送到已有分支 {branch}"
                  f"（基点 = {base_sha[:8]}）")
        if opts["new_task"] and state.get("task_branch") \
                and state["task_branch"] != branch:
            # 旧分支不再由本次流程接管：从 tasks 里摘掉，让它变成孤儿分支，
            # 由 --prune 报告并给出删除命令（比悄悄留着更可控）。
            #
            # A6-J2：**只登记，不在这里改 tasks、也不打印**。
            # 原实现在此处 pop + 打印「已不再接管」，而此处到第一次
            # save_state() 之间有 8 个提前退出点（取消 / 无事可做 /
            # dry-run）。状态文件从未写回时，磁盘上 task_branch 仍是旧分支：
            #   · 下次运行又回到旧分支（用户以为已经弃用了）
            #   · 而它已被本次「宣布」为孤儿，--prune 会报它
            #   → 状态与输出互相矛盾。--dry-run 尤其明显：打印了一条
            #     变更消息，而预演不落盘，实际什么都没变。
            #
            # 真正「不再接管」的时点是**本次推送确实完成**之后
            # （见 _main 末尾 _apply_orphaned_task）。推迟登记后，
            # 取消/无改动/dry-run 一律不产生这条声明，也就无从矛盾。
            global _ORPHANED_TASK_OLD
            _ORPHANED_TASK_OLD = state["task_branch"]
        state["task_branch"] = branch
        # M86：同上，改走 Ui 通道 —— json 模式进 events（信息不丢），
        # 文本模式逐字不变。这是 M86 第三个场景（无待推送改动）里
        # 挡在 JSON 前面的最后一行裸 print。
        _ui_now = get_ui()
        if _ui_now is not None:
            _ui_now.line(f"任务分支 {branch} = {str(bhead)[:8]}"
                         f"（PR 目标 {BRANCH}）")
        else:
            print(f"任务分支 {branch} = {str(bhead)[:8]}（PR 目标 {BRANCH}）")

    # -------- 收集待推文件 --------
    # PR 工作流下要扩展为「本地 vs **分支远端**」的差异全集：
    # 只推用户改动的文件，分支里残留的旧版本会让 PR diff 显示成
    # 「把主干的改动改回去」，合并时就被当成你故意回退而直接采纳 —— 静默覆盖。
    #
    # 还要并入 baseline_changed_files()：git status 的对比基准是本地 HEAD，
    # 用户先 `git commit` 到本地之后工作区就干净了，status 什么都报不出来，
    # 改动会静默卡死（git 协议被网关拦成 403 时，用户的直觉反应就是先本地提交）。
    # N86：点名路径原样透传、不去重，而自动路径走 set() 去重 ——
    # 同一文件用 `a.txt ./a.txt sub/../a.txt` 三种写法会被推三次。
    # 规整（safe_rel）在**之后**才做，所以这里必须显式去重。
    if explicit:
        # N368（第一百一十七轮，中危）：上面那句"这里必须显式去重"
        # **说了没做** —— 判据用的是**原始串** `_f`，于是
        # `a.txt` / `./a.txt` / `sub/../a.txt` 三种写法是三个不同字符串，
        # 一个都去不掉，同一个文件被推三次（三次建 blob、三次进 tree）。
        #
        # 注释逐字列出的三种写法，修法一条都没覆盖。
        #
        # 去重必须按**规整后的路径**：safe_rel() 把 ./ 与 ../ 归一化掉，
        # 那才是"这个文件到底是谁"。原始串只是"用户怎么写的它"。
        #
        # 保留第一个写法（用户最先写的那个）作为候选 —— 后续流程还会
        # 再跑一次 safe_rel 并落 git，所以这里换不换串都等价，
        # 但保留原串能让报错里的路径与用户输入一致。
        _seen_c = set()
        _dedup = []
        for _f in explicit:
            _k = safe_rel(_f)
            if _k is None:
                _k = _f          # 非法路径按原串判重，仍会进 rejected
            if _k not in _seen_c:
                _seen_c.add(_k)
                _dedup.append(_f)
        candidates = _dedup
    else:
        candidates = sorted(
            set(detect_changes()) | set(baseline_changed_files(state, lmap)))
    files, rejected, dropped = [], [], []
    unencodable = []
    _rej_link = []          # M101：软链被拒（与真越界分开报）
    for f in candidates:
        rel = safe_rel(f)
        if rel is None:
            # M101（A102，dogfood 实测）：这里报「越出仓库范围或非法」，
            # 而**悬空软链**既不越界也不非法 —— git 完全支持它
            # （mode 120000，blob 内容就是目标路径字符串，
            #   实测 `git add` rc=0、`git status` 显示 `?? dangling.txt`）。
            #
            # 被拒的真实原因是 safe_rel() 用 realpath 判越界，而悬空链接的
            # realpath 解析到**不存在的目标**，自然"不在仓库内"。
            #
            # 那个判据本身是对的（它防的是 `etcdir -> /etc` 这类
            # 「路径穿过软链读到仓库外内容」），只是恰好也把悬空链接
            # 归进了同一类 —— 于是用户拿到的是一句不成立的诊断，
            # 外加「检查拼写」这个完全不着边际的建议。
            #
            # 不改判据（是否推送软链是产品取舍），只把**原因**说出来：
            # 把链接的目标路径一并打印，用户一眼就知道发生了什么。
            _f = f
            try:
                if not os.path.isabs(_f):
                    _lp = os.path.join(ROOT, _f)
                    if os.path.islink(_lp):
                        _rej_link.append("%s → 软链指向 %s"
                                         % (_f, os.readlink(_lp)))
                        continue
            except OSError:
                pass
            rejected.append(f)                  # 越界 / 非法
            continue
        # GitHub API 的 payload 是 JSON，只接受 UTF-8 字符串。
        # 含 surrogate 的路径（Linux 上文件名是任意字节，可能是非法 UTF-8）
        # 到 json.dumps 那一步才会炸 —— 而那时分支已在远端建好，
        # 崩完留下没人管的僵尸分支。必须在建分支之前挑出来。
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:
            unencodable.append(rel)
            continue
        full = os.path.join(ROOT, rel)
        if os.path.isfile(full) or os.path.islink(full):
            files.append(rel)
        else:
            # 显式点名却不存在，或是目录。过去二者都被静默丢掉，
            # 打到「本地没有待推送的改动」上 —— 用户无法区分
            # 「真的没改」和「我把文件名打错了 / 我点的是目录」。
            dropped.append(f)
    # N369：上面是否已报过「点名落空」。
    # M121（A113，P3）：原名 `_dropped_reported`，只在 `if dropped:` 里
    # 置位 —— 而下面收尾的判据是 `rejected or dropped`，于是 **rejected
    # 单独命中**时上面报了、下面照旧再报一遍：
    #     ❌ 以下路径越出仓库范围或非法，已忽略：['../outside.txt']
    #     ❌ 指定的路径都不存在或不是普通文件，已忽略：['../outside.txt'] ★
    #     检查拼写；目录需要写成目录下的具体文件。                     ★
    # 第二条不只是重复 —— 措辞与第一条**矛盾**，且给的动作（检查拼写）
    # 完全不着边际：路径不是拼错，是越界。改名并让两类都置位。
    _named_reported = False
    if unencodable:
        show = [p.encode("utf-8", "replace").decode("utf-8", "replace")
                for p in unencodable[:5]]
        print(f"\n❌ {len(unencodable)} 个路径含非 UTF-8 字节，"
              f"无法经 API 推送，已忽略：{show}")
        print("   请把这些文件改名为 UTF-8 文件名后再推。")
    if _rej_link:
        # M101：与真越界分开报 —— 二者的成因、用户该做的动作完全不同。
        print(f"\n❌ 以下是符号链接且目标不在仓库内，已忽略：{_rej_link}")
        print("   git 对软链只保存「目标路径字符串」（mode 120000），"
              "推上去等于把它公开；目标不存在即悬空链接。")
        print("   确要推送就把链接换成普通文件，或让目标落在仓库内。")
    if rejected:
        print(f"\n❌ 以下路径越出仓库范围或非法，已忽略：{rejected}")
        # M121：越界也算「上面已报过点名落空」，否则下面收尾会叠加一条
        # 措辞相反的「检查拼写」。
        _named_reported = True
    if dropped:
        # 部分命中时也要说：点了三个文件、推上去两个，用户往往察觉不到，
        # 直到后来发现第三个文件根本没上远端。
        print(f"\n❌ 以下路径不存在或不是普通文件，已忽略：{dropped}")
        print("   检查拼写；目录需要写成目录下的具体文件。")
        # N369（第一百一十六轮，低危）：点名**全部**落空时，下面收尾处
        # 还会再打一遍几乎相同的「指定的路径都不存在…」。两条语义重叠、
        # 提示行逐字重复 —— 用户点名一个目录是高频误操作，最常见形态下
        # 会看到四行重复内容。与 N363（--prune --json 的「暂不支持」
        # 打印两次）同形：两处互不知情各加了一条。
        _named_reported = True

    # extra 必须在条件块**外**初始化：
    # 原写法只在 `wf == "pr" and not explicit` 分支里赋值，于是
    # direct 工作流或点名推送时，下面 _will_push 引用它会抛
    # UnboundLocalError —— 实测开启 --delete 后 10 套测试直接崩。
    # 而删除默认关闭，所以常规回归全绿，这个崩溃完全没暴露。
    extra, skipped_stale = [], []

    if wf == "pr" and not explicit:
        brstate = remote_state_map(base_sha)

        for p in brstate:
            if lmap.get(p) == brstate[p]:
                continue                    # 本地已与分支一致
            if not (os.path.isfile(os.path.join(ROOT, p))
                    or os.path.islink(os.path.join(ROOT, p))):
                continue
            # 关键：本地**没改过**、只是落后的文件，不能进 extra。
            #
            # 新分支基点就是最新主干，分支里本不可能残留旧版本 ——
            # 这里 "lmap != brstate" 其实是「本地旧」，不是「分支旧」。
            # 把它推上去，等于把主干回退成本地的旧版本；GitHub 三方合并时
            # theirs==base、ours 变了 → 直接采纳 ours，静默回退他人改动。
            # 分支保持主干内容即可，本地要更新应当走 --pull。
            if _local_matches_baseline(state, lmap, p):
                skipped_stale.append(p)
                continue
            extra.append(p)

        if skipped_stale:
            print(f"  · {len(skipped_stale)} 个文件你没改过、只是本地版本旧，"
                  f"不纳入本次推送（否则会把主干回退）")
            print(f"    需要更新本地请跑 --pull：{sorted(skipped_stale)[:5]}"
                  f"{' …' if len(skipped_stale) > 5 else ''}")
        if extra:
            # 措辞必须准确：这里同步的是「分支上仍是旧版本的文件」，
            # 目的是避免 PR diff 出现回退；**不是**把主干的更新合进你的
            # 本地工作区。旧文案说成「同步主干」，而 --hold 复用分支时
            # 根本没有这个逻辑（base 仍是分支头）—— 用户信了这句提示
            # 就不会去 --pull，直接 --merge 撞上冲突判定迷宫。
            print(f"  · {len(extra)} 个文件在分支上还是旧版本（主干已更新或首次推送），"
                  f"一并写进分支，避免 PR diff 出现回退")
            # 必须**列出文件名**：只给个数字的话，用户看不出这批里
            # 哪些是自己改的、哪些是「被带上的」。协作仓库里后者可能
            # 夹杂别人刚推的内容 —— 用户没法核对，也就没法发现异常。
            # 实测反馈：全量推送被带进 7 个文件，而只有知道「要点名」
            # 这个技巧的人才能避开，等于防护依赖使用者经验。
            for p in extra[:10]:
                print(f"   - {p}")
            if len(extra) > 10:
                print(f"   … 其余 {len(extra) - 10} 个")
            print("     注意：这**不是**把主干更新合进你的本地文件；"
                  "要让本地跟上主干请跑 --pull。")
            print("     不确定是否该带这些文件时，改用点名推送即可只推你自己改的：")
            print("       python3 push_api.py -m \"...\" <文件路径>")
            files = sorted(set(files) | set(extra))

    # -------- M92（A97）：副产物进清单要在**上传前**拦下 --------
    #
    # 放在 `files` 最终确定之后（含 extra）、上传之前：此刻拿到的才是
    # 真正会进 commit 的清单。放在更早处看不到 extra，放在上传后就晚了
    # —— 明文已经在远端了。
    #
    # 为什么是 `_blocked()`（退出码 2 = 被防护拦下、改动未丢）而不是
    # 只打印警告：脚本给**审计流水**（只是运行日志）做了三层防护
    # （N303 改判据 / N320 分措辞 / N321 修子串误判），还专门写着
    # "这正是 #108 要防的事故"；而装着**完整远端明文 + 合并基点**的
    # 副产物此前**零防护**。日志防三层、明文零防护，这个不对称本身
    # 就是该修的那一类。
    #
    # 用 `_blocked()` 与 N361 收敛的那批同形态（调用即抛，不写 raise）。
    if files:
        # N557：先把「已知冲突路径」的**确切**副产物名登记进去，再判。
        # 顺序不能反 —— 登记是 `_conflict_artifacts_in` 覆盖旧编码残留
        # 那一格的唯一依据（它拒绝用后缀猜，怕误伤用户文件）。
        register_known_artifact_names(state)
        _arts_in_push = _conflict_artifacts_in(files)
        if _arts_in_push:
            _blocked(
                f"冲突副产物进了待推送清单，已中止（改动都没丢）。\n"
                f"   {len(_arts_in_push)} 个：{_arts_in_push[:5]}"
                f"{' …' if len(_arts_in_push) > 5 else ''}\n"
                f"   里面是**远端文件的完整明文**（还有一份合并基点），"
                f"推上去等于把它们发布到远端仓库。\n"
                f"   成因：副产物目录落在 git 仓库内、未被 .gitignore 屏蔽，"
                f"于是被 detect_changes() 正常拾取。\n"
                f"   处理（任选其一）：\n"
                f"     · 确认无用后删掉整个副产物目录\n"
                f"     · 或把它加进 .gitignore（保留在本地供人工比对）\n"
                # N558（第一百八十二轮，低危）：上面那条「加进 .gitignore」
                # 对**已被 git 跟踪**的文件无效 —— 而能被这里拦下的文件恰恰
                # 都是已跟踪的（否则不会进待推送清单）。git 对已跟踪文件
                # **不应用** .gitignore。实测：已 `git add` + 已写 .gitignore
                # → `git check-ignore` 未命中、照建议做后重跑**仍被拦**，
                # 退出码恒 2，推送被永久卡住；撤出索引后同一份 .gitignore
                # 才生效。真正有效的那一步是 `git rm --cached`，原提示
                # 一次都没提 —— 而同一条输出下方审计流水那处的提示就写着
                # 「（该文件尚未入库，无需 git rm）」，说明作者清楚"已入库"
                # 与"未入库"要分开说，只是这条没跟上。
                f"       注意：.gitignore 只对**未跟踪**文件生效；"
                f"已被 git 跟踪的要先 `git rm --cached <路径>`。\n"
                
                f"   注意：--clean-conflicts 只清**新**目录"
                f"（<ROOT>/.git/push-conflicts），旧位置残留要手工处理。")

    # -------- .gitignore 屏蔽文件的提示（P1）--------
    # 「点名即意图，等同 git add -f」这个语义可以辩护，但必须让人知道代价：
    # 这类文件此后**不受自动检测**——git status 被 .gitignore 屏蔽，
    # local_state_map 基于 git ls-files 也看不到它，于是后续改动会静默漏推。
    # 这与当初修的「已 commit 推不上」是同一类静默漏推。
    #
    # ignored_files 必须在**这里就初始化**：收尾补提交要拿它过滤路径，
    # 而 `if files:` 为假时下面那个块不执行 —— 那时它得是空集合，
    # 不能留个未定义名（files 为空意味着根本走不到收尾，但依赖「走不到」
    # 来保证安全，本身就是脆弱的隐式契约）。
    ignored_files = set()
    if files:
        # 放在 files 最终确定之后：此时才包含 extra（分支上残留的旧版本文件）。
        warn_if_filters_active(files)
    if files:
        # M187：裸调 → check-ignore 失败时异常穿透到顶层（裸 traceback）。
        ignored = sorted(_gitignored_safe(files)[0])
        # 存成集合留给收尾用：本地 git add / commit **必须排除**这批路径，
        # 否则一个被 .gitignore 屏蔽的文件会让整批提交失败
        # （实测：75 个文件里 1 个被屏蔽 → 75 个一个都没提交，
        #  而 git 的报错只是 pathspec 不匹配，看不出真正原因）。
        #
        # 远端层面的推送**照旧带上它们** —— 「点名即意图，等同 git add -f」
        # 是本脚本既定的语义，这里只是不让本地 git 操作被它们拖垮。
        ignored_files = set(ignored)
        # N12：ignored 只对 files（基线内）计算，新增路径恒不在其中，
        # 传给插件的 `lambda p: p in ignored` 对新增路径恒 False
        # → 新增侧等于没有 .gitignore 保护。根因在调用方，
        # 模块内怎么过滤都无效（M1 的建议因此不成立）。
        try:
            _lmap_now = local_state_map()
            ignored_files |= set(gitignored_set(
                [q for q in _lmap_now if q not in ignored_files]))
        except Exception:
            pass          # 拿不到就维持原集合，不阻断主流程
        if ignored:
            if explicit:
                # 点名 = 明确意图，等同 git add -f，照推。
                # 但必须说清代价，尤其是**远端已有一份**的情形：
                # 那不是「新增一个运行时文件」，而是用本地旧快照覆盖别人的
                # 当前内容（实测：别人的审查进度文件 _common/skills/
                # code-audit/.audit-state.json 差点被我几小时前的旧副本盖掉，
                # 全程零报错 —— 因为它在 .gitignore 里，本地 git 看不见，
                # 第一层校验也无从判断「本地是否落后」）。
                print(f"\n  ! {len(ignored)} 个文件被 .gitignore 屏蔽，本地 git 不跟踪："
                      f"{ignored[:5]}{' …' if len(ignored) > 5 else ''}")
                have_remote = [p for p in ignored if p in rstate]
                if have_remote:
                    print(f"  ⚠ 其中 {len(have_remote)} 个远端**已存在**，"
                          f"本次会用本地内容覆盖远端版本：")
                    for p in have_remote[:5]:
                        print(f"   - {p}")
                    if len(have_remote) > 5:
                        print(f"   … 其余 {len(have_remote) - 5} 个")
                    print("   本地这份是旧快照还是最新，脚本无法判断（git 不跟踪它）。"
                          "确认后再推。")
                print("    推上去之后，这类文件的改动不会被自动检测到，"
                      "需要再次显式点名推送。")
            else:
                # 默认安全：自动检测集里的被忽略文件一律**不推**。
                #
                # 它们混进来的路径是：远端有、本地被 gitignore →
                # init_baseline 归进 missing 记了基线 → 全量推送时又被
                # 「分支上还是旧版本，一并写进分支」的逻辑带上。
                # 结果是推一个**连本地 git 都不该管**的运行时文件，
                # 而且没人点过它的名 —— 与「点名即意图」的语义正好相反。
                #
                # 要推可以，点名即可（走上面的分支）。这让默认行为是安全的，
                # 而不是只在打印里说明代价 —— 说明代价防不住误推，
                # 只会在事后让人知道自己是怎么踩的。
                files = [p for p in files if p not in ignored_files]
                print(f"\n  · {len(ignored)} 个文件被 .gitignore 屏蔽，"
                      f"**未纳入**本次推送：")
                for p in ignored[:5]:
                    print(f"   - {p}")
                if len(ignored) > 5:
                    print(f"   … 其余 {len(ignored) - 5} 个")
                print("   它们不是你点名的改动，且本地 git 不跟踪，"
                      "推上去容易覆盖别人的内容。")
                print("   确要推送请显式点名（等同 git add -f）："
                      "python3 push_api.py -m \"...\" <文件路径>")

    # N48：删除探测**不能只在 files 为空时做**。
    # 原写法把唯一的赋值放在 `if not files:` 之内，于是 files 非空时
    # _pending_dels 恒为 [] —— 而下方 `if not todo: if not _pending_dels: 取消`
    # 那道「有删除就不早退」的保护正依赖它，保护因此完全失效。
    #
    # files 非空而 todo 为空是可达的：FileBaselineGuard 会把「本地与远端
    # 一致」的文件跳过（`· x 已与远端一致，跳过`），此时若有删除待落实，
    # 原逻辑会把它取消掉 —— 用户看到「没有需要推送的内容」，
    # 而他的删除根本没被处理。
    #
    # 探测成本：一次 git status，且与删除功能是否被关无关（关了也要知道
    # 「本来有删除」，否则 --no-delete 下无法区分「真没改动」）。
    _pending_dels = []        # 本次要落实的删除路径（纯删除时也靠它继续往下走）
    _dels_all = _detect_deletions()
    # N63：门控用的 _pending_dels 是**过滤前**的值，而模块内还会再过滤
    # （gitignore + 越界）。于是 entries 可能为空而 _pending_dels 非空
    # → 早退判断用前者 → 照样建分支、建空 tree、推空提交。
    # 实测：删除一个"基线里有、但被 .gitignore 屏蔽"的文件，
    # entries=[] skipped=[] 而 _pending_dels 非空 → 空提交。
    # 这里按同一口径过滤，让门控与模块看到同一份清单。
    #
    # N247（第七十二轮，中危）：过滤**必须放在下面 N16 那一路并入之后**，
    # 不能停在这里。理由：committed（已 commit 的删除）既不在 files 里
    # 也不在 lmap 里，它只从 `collect_removal_candidates()` 来 ——
    # 在这里过滤，等于只对 git status 那一路生效，committed 全漏。
    #
    # 漏了它的实测后果（当前代码仍在的状态）：
    #   · 预览/预检的 gitignored 回调口径 = files ∪ lmap  → 放行
    #   · 正式执行的口径 = files ∪ _pending_dels          → 静默过滤
    #   同一批被 .gitignore 屏蔽的已删路径，预演说"会删"、
    #   正式一个都不删，且正式侧连 skipped 都没有（ignore() 在判据之前，
    #   不产生 skip 记录）—— 而脚本自己在 N30 注释里写过：
    #   「静默漏删比拦下危险，因为它无法被发现」。
    #
    # 放到合并之后，_pending_dels 本身口径就对了，门控 / 预检 / 预览 /
    # 正式四处自动一致（都不删，且打印出来）。
    # N16：门控原来只有 `_detect_deletions()`（git status）这一路信号，
    # 而它对**已 commit 的删除返回空**（工作区干净就一个字都没有）。
    # 能抓到已 commit 删除的 `collect_removal_candidates()` 在 push_api 中
    # 出现 0 次 —— 于是「删了文件并 commit」的用户推送时，
    # 门控判定「没有待推送的内容」直接 return，删除永远等不到执行。
    #
    # 补上这一路。
    #
    # N118/N127：这里原本是
    #   `importlib.import_module("delete_rename_support")`
    # —— 从 sys.path 加载外部文件，而 sys.path 含脚本所在目录。
    # 模块已合并进单文件（#28/#29），这段却**一字未动**：
    # 于是"合并消除任意代码执行面"的结论是假的 ——
    # 仓库里放一个同名 .py 仍会被 import 执行，而且 #29 把开关删了，
    # 现在连"默认不加载"这层保护都没有了。
    #
    # 直接调本文件内的函数。collect_removal_candidates 就在上面的
    # 「删除与重命名」区块里，无需任何动态加载。
    if _plan_removals is not None:
        try:
            _committed = collect_removal_candidates(state, local_state_map())
            for _q in _committed:
                if _q not in _dels_all:
                    _dels_all.append(_q)
        except Exception:
            pass          # 拿不到就维持原样，不阻断主流程
    # N63 / N247：gitignore 过滤放在**两路信号合并之后**（见上方 N247 注释）。
    # 在这里过滤，_pending_dels 本身就是干净的，门控与模块看到同一份清单。
    #
    # N247 的第二半：被滤掉的这批路径必须**留在回调口径里**。
    # 否则会出现新的自相矛盾 —— 上面打印「不计入本次删除」，
    # 而 `plan_removals` 内部会自行 `merged.extend(committed)`
    # 把 committed 那一路加回来（调用方传的 deleted 过滤了也没用），
    # 于是它照样生成删除条目：打印说排除、行为照删。
    #
    # 所以：_pending_dels 排除它们（门控不把它们算成待删、不产生空提交），
    # 同时 `gitignored` 回调对它们返回 True（模块内部也删不掉），
    # 三处调用点共用同一份口径 —— 既一致，也看得见（上面那行打印）。
    _dels_ignored = set()
    # M187：原为 `except Exception: pass` —— 不崩，但过滤**静默**失效。
    # 改走 _gitignored_safe：与另两处同源，失效时可见（仍按「未屏蔽」处理，
    # 不把 fail-open 改成 fail-closed —— 那是另一个产品决策）。
    # N610：删除侧必须 `--no-index`（理由见 gitignored_set 的 docstring：
    # 不加时"算不算被屏蔽"取决于它此刻还在不在 git 索引里）。
    _ig_now, _ig_ok = _gitignored_safe(_dels_all, no_index=True)
    if _ig_now:
        print(f"  · {len(_ig_now)} 个删除路径被 .gitignore 屏蔽，"
              f"不计入本次删除：")
        for _p in sorted(_ig_now)[:5]:
            print(f"      - {_disp_path(_p)}")
        # N609（第二百〇一轮，低危）：同一函数里 `_dels_blocked` 与「点名
        # 范围外」两处都有「… 其余 N 个」，唯独这句没有 —— 实测 7 个里
        # 用户看见 5 个，且不知道少了 2 个。
        if len(_ig_now) > 5:
            print(f"      … 其余 {len(_ig_now) - 5} 个")
        # N608（同轮，中危）：这批路径**没有任何处置通道** —— 实测三条路
        # 全部无效：点名不覆盖（过滤发生在 `if explicit:` 之前，被滤掉的
        # 进不了 `_dels_outside`）、`--force-delete` 不覆盖（`plan_removals`
        # 的 `ignore()` 与 force 参数无关）、也没有开关能关掉这道过滤。
        # 于是它们是永久幽灵：远端删不掉、`--status` 一直报、提示里没出路。
        # 而脚本对新增侧的既定语义恰恰相反（「点名即意图，等同
        # git add -f，照推」），删除侧这条不对称此前一字未提。
        print("     点名与 --force-delete 都覆盖不了这道过滤；要真正清掉"
              "远端副本，需先让这些路径不再匹配 .gitignore"
              "（在 .gitignore 里加 `!路径` 例外，或删掉对应规则），再重推。")
        # N611（第二百〇二轮，中危）：本句在 `_deletes_disabled()` 判定
        # **之前**、且完全不看它；而 `_dels_all` 紧接着就被清空，于是下面
        # `if _dels_all and _deletes_disabled():` 那整段（"仍留在远端" +
        # "怎么开"）**永远进不去** —— 默认配置（删除关闭）下，被屏蔽场景
        # 读到的唯一一句是本句，最后一句是「本地没有待推送的改动」，
        # 用户会以为这件事已经了结。
        # 对照：同样是"被 .gitignore 屏蔽"、只是用了 `rm` 的场景反倒能拿到
        # 完整提示 —— 提示完整度取决于用户用的删除方式，正是 N610 那条
        # 裂缝在告知侧的投影。
        if _deletes_disabled():
            print("     删除功能当前**关闭**：这些路径**仍留在远端**，"
                  "且即使本次开启也不会删（过滤与开关无关）。")
    _dels_ignored = set(_ig_now)
    _dels_all = [p for p in _dels_all if p not in _ig_now]
    # N612（同轮，低危）：`git rm --cached <f>`（取消跟踪但**保留文件**）
    # 也造出 N63 那个形状（`_pending_dels` 非空而 `plan_removals` 的
    # entries 为空 → 不早退 → 建分支 → 一条删除都不生成），而 N63 的过滤
    # 只认 .gitignore 这一种成因，认不出"本地仍存在"这一种。
    # 实测：`_detect_deletions()` 返回 ['keep.txt']，而
    # `plan_removals` 把它列为 skipped「本地仍存在，判定为误报，不删」。
    # 门控与执行同源地剔除本地仍在的路径。
    _dels_still_here = [p for p in _dels_all if _local_exists(p)]
    if _dels_still_here:
        print(f"  · {len(_dels_still_here)} 个删除路径**本地仍存在**"
              f"（如 `git rm --cached` 取消跟踪但未删除文件），本次不处理：")
        for _p in _dels_still_here[:5]:
            print(f"      - {_disp_path(_p)}")
        if len(_dels_still_here) > 5:
            print(f"      … 其余 {len(_dels_still_here) - 5} 个")
        _dels_all = [p for p in _dels_all if p not in _dels_still_here]
    # 预览/预检的回调口径（`ignored_files`）原本只覆盖 files ∪ lmap，
    # 而删除路径不在这两者里（已删）。补上被屏蔽的删除候选，
    # 让三处 `gitignored=` 看到同一份集合（N247）。
    ignored_files |= _dels_ignored
    # N49：点名推送只约束新增/修改，删除是**唯一**不受 `explicit` 约束的
    # 变更类型（5852–6057 区间内 explicit/allowed 出现次数均为 0）。
    # 后果：`python3 push_api.py --delete -m "修个 typo" README.md`
    # 会一并删除本地**所有**已删除文件，数量不限。
    #
    # 这与脚本自己针对 .gitignore 文件声明的原则正好相反：
    # 「没人点过它的名」的变更不该被执行 —— 而删除比多推一个文件更不可逆。
    #
    # N370（第一百一十七轮，中危）：上面这段**过滤生效了，却被它下面
    # 20 行的 N16 追加整段推翻** —— N16 无条件把
    # `collect_removal_candidates()` 的结果追加回 `_dels_all`，
    # 而后者看的是"基线有、lmap 无"，**未 commit 的 git rm 也满足**。
    # 更后面又有一句 `_pending_dels = _dels_all`（重新赋值），
    # 于是过滤形同虚设。
    #
    # 决定性对照（基线 a.txt/b.txt，本地删 b.txt，只点名推 a.txt）：
    #     打印"1 个删除不在点名范围内，本次不处理（仍留在远端）"
    #     ★ 实际 b.txt 仍被删了
    # **同一份输出：上面说"不处理、仍留在远端"，下面说"将删除 1 个"。**
    #
    # 两条修复互相抵消，与 N308（判据 5 抵消 N240）同族。
    # 修法：**先合并两路信号，再一次性过滤** —— 即把 N16 块上移到本块
    # 之前（见上方），而不是在本块之后再补救。
    if explicit:
        # N536（第一百七十六轮，中危）：`allowed` 一直用**命令行原始串**，
        # 而模块内部比的是 `safe_rel` 归一化后的路径 —— 同一个文件写成
        # `./b.txt` / `sub/../b.txt` 就不在范围内了。实测（点名 a.txt
        # 与 ./b.txt，deleted=['b.txt']）：原始串 → 打印「1 个删除不在
        # 点名范围内」★ 实际删=[]；且用户照提示把 ./b.txt 再写一遍，
        # 同一句提示原样再来 —— 闭环死结。同一文件里 --force-file
        # 的放行清单早就走 safe_rel，两个口径。
        #
        # N537（同轮，中危）：上面刚打印「以下路径…已忽略：['b.txt']」
        #（dropped / rejected / 符号链接 / 非 UTF-8），而 `_allowed` 用
        # 的是**未经那层过滤**的 explicit —— 于是同一份输出里先说
        # 「已忽略」、后说「将删除 1 个」。删除是唯一不可逆的远端操作，
        # 对不可逆操作先声明「不管它了」再照做，比不提示更糟
        #（用户看到「已忽略」就不会再去确认）。
        _allowed = {_r for _r in (safe_rel(_f) for _f in explicit) if _r}
        for _ig in (rejected, dropped, unencodable, _rej_link):
            _allowed -= {_r for _r in (safe_rel(_f) for _f in _ig) if _r}
        _dels_outside = [p for p in _dels_all if p not in _allowed]
        if _dels_outside:
            print(f"  · 点名推送：{len(_dels_outside)} 个删除不在点名范围内，"
                  f"本次不处理（它们仍留在远端）：")
            for _p in _dels_outside[:10]:
                print(f"      - {_p}")
            if len(_dels_outside) > 10:
                print(f"      … 其余 {len(_dels_outside) - 10} 个")
            print("     要一并删除请把这些路径也写在命令行上。")
            # N559（第一百八十三轮，中危）：`_dels_outside` 只裁了**执行侧**
            #（`_dels_all` / `_pending_dels`），而 `report_moves` 早已把
            # `old.txt` 登记进了 `_RUN_PENDING_DELETES`（emit=False、
            # judged=False）。运行出口重算时口径**不含 allowed**，于是
            # 又把它算回来、并断言「本次推送会真的删除」。
            #
            # 于是同一次输出里三句话三个方向：
            #   ① report_moves：「这些旧路径将在本次推送中删除」
            #   ② 本处：「不在点名范围内，本次不处理（它们仍留在远端）」
            #   ③ 出口清单：「本次推送会真的删除上述路径」
            # 而 ② 才是事实（执行侧已为空）。用户按 ③ 去核对"删干净没"，
            # 会发现文件还在，于是怀疑脚本没删成功 —— 与 N175 描述的
            # 用户反应完全一致。② 与 ③ 相隔不到 20 行，不用翻历史就能
            # 同时看到。
            #
            # 方向是 fail-closed（不会误删），故中危不高危。
            _trim_pending(_dels_outside)
        _dels_all = [p for p in _dels_all if p in _allowed]
        _pending_dels = [] if _deletes_disabled() else _dels_all

    _dels_blocked = _detect_deletions_skipped()
    if _dels_blocked:
        # N56：这些路径被起飞前消毒挡掉（越界 / 非 UTF-8），
        # 原先静默丢弃。删除是唯一不可逆的操作，静默丢弃等同于
        # 「工具没做」—— 用户会一直困惑为什么删了没反应。
        print(f"  ! {len(_dels_blocked)} 个删除路径无法通过路径校验，已排除：")
        for _p in _dels_blocked[:5]:
            print(f"      - {_p!r}")
        if len(_dels_blocked) > 5:
            print(f"      … 其余 {len(_dels_blocked) - 5} 个")
        print("     原因：路径越界（../ 或绝对）或含非 UTF-8 字节。"
              "非 UTF-8 文件名请改名后再删。")
    _base_now_early = (state.get("files") or {})
    _dels_all = [p for p in _dels_all if p in _base_now_early]
    _pending_dels = [] if _deletes_disabled() else _dels_all

    # N496：混合场景（有改动 **同时** 有删除）在这里问。
    #
    # 为什么是两处：清单原本只在下面 `if not files:` 内打印（纯删除），
    # 另一处（15674 附近）在推送**之后**才打 —— 那时 tree 已建完、内容
    # 已推上去，再问「要不要删」只能再跑一次推送。
    # 而最常见的形态恰恰是混合场景，于是最需要问的那一次反而问不到。
    #
    # 这里处在「_pending_dels 已算完」与「建 tree」之间，是最后一个
    # 还能让删除跟本次推送一起生效的位置。
    print('[DBG] files=%s dels_all=%s disabled=%s' % (len(files) if files else 0, _dels_all, _deletes_disabled()))
    if files and _dels_all and _deletes_disabled():
        if _offer_delete_now(_dels_all, dry):
            _pending_dels = _dels_all

    if not files:
        # 「纯删除」也必须算有东西要推。
        #
        # detect_changes() 会把 "D" 挡在待推送清单之外（已删文件没有本地
        # 内容可读，放进 todo 会在建 blob 时读不到文件而失败），于是只删
        # 文件不改文件的时候 files 为空 → 若在这里直接 return，删除就
        # **永远等不到执行的机会**（实测复现）。
        #
        # 所以确有删除时不早退：files 保持为空，下面的建 blob 循环不跑，
        # 只有删除条目进 tree —— 语义上正是一次「仅删除」的提交。
        # 删除探测始终跑（即便功能被关掉也要知道「本来有删除」），
        # 否则 --no-delete 下用户看到的是「本地没有待推送的改动」，
        # 与「真的没改动」在输出上无法区分 —— 他会以为删除已经生效了。
        # _dels_all / _pending_dels 已在上面（if 之外）算出并过滤过，
        # 这里直接用 —— 见 N48 的说明：放在 if 内会让 files 非空时恒为空。
        if _dels_all and _deletes_disabled():
            # 关闭时也要**精确**说清：不能只说「有 N 个删除」。
            # 用户据此要能判断「这几个我是真想删吗」，所以逐个列出，
            # 并点明它们此刻在远端仍然存在（删的是本地，远端没动）。
            print(f"\n  ! 本地已删除 {len(_dels_all)} 个文件，"
                  f"但它们**仍留在远端**（删除功能默认关闭）：")
            for _p in _dels_all[:20]:
                print(f"      - {_p}")
            if len(_dels_all) > 20:
                print(f"      … 其余 {len(_dels_all) - 20} 个")
            print("     确认要一并删除远端副本：")
            _print_how_to_enable(7)
            # N496：就地问一次。答 y 就本次生效，不必记住开关再重跑命令。
            if _offer_delete_now(_dels_all, dry):
                _pending_dels = _dels_all
        if not _pending_dels:
            # 显式点名却一个都没匹配上时，必须说清楚：否则「打错文件名」
            # 和「真的没有改动」在输出上完全一样，用户只能靠猜。
            if _named_reported:
                # N369：上面刚逐字报过一遍，这里再报就是重复噪音。
                # 仅在上面**没**报过时（走了别的分支）才补 —— 宁可多报。
                # M121：越界（rejected）时上面也已报过，同类处理。
                pass
            elif rejected or dropped:
                print(f"\n❌ 指定的路径都不存在或不是普通文件，已忽略："
                      f"{sorted(set(rejected) | set(dropped))[:10]}")
                print("   检查拼写；目录需要写成目录下的具体文件。")
            elif unencodable:
                print("   （唯一的推送候选含非 UTF-8 字节，见上方提示。）")
            # N18：有删除悬着时不能说「没有待推送的改动」。
            # 上面刚打印了「本地已删除 N 个文件，但仍留在远端」，
            # 紧接着一句「没有待推送的改动」恰好制造了注释里担心的
            # 那种混淆 —— 用户会以为删除已经生效。
            # M86 第三个场景（CI 最常见：`--json -m x --yes` 而本地无改动）：
            # 报告里三个 ★ 场景，前两个由上面的「远端文件 N 个」静音 +
            # init_baseline 的 json 分支解决，第三个落在**这里**。
            # 这是推送路径的早退，同样是 rc=0 + 纯文本 —— 消费者拿到
            # 空输入会当成「跑成了但没数据」。
            if _json_active():
                print(json.dumps({
                    "ok": True, "reason": "nothing_to_push",
                    "action": "push",
                    "changed": [], "count": 0,
                    "pending_deletes": list(_dels_all),
                    "pending_delete_count": len(_dels_all),
                }, ensure_ascii=False))
                _mark_json_emitted()
            elif _dels_all:
                print("\n本次不会推送任何内容（远端保持原样）。")
                print(f"   上述 {len(_dels_all)} 个删除仍未在远端生效。")
            else:
                print("\n本地没有待推送的改动。")
            # git stash 之后工作区变干净，改动在 stash 里而非消失。
            # 不提示这句，用户会以为改动丢了（实测：他自己忘了 stash 过）。
            hint = _stash_hint()
            if hint:
                print(hint)
            return
        # 有删除：不早退，继续往下走（files 仍为空，只会有删除条目）。
        print(f"\n  · 本地没有文件改动，但有 {len(_pending_dels)} 个删除待落实。")

    # -------- 真正创建任务分支 --------
    # 到这里才 POST：确认有东西要推，且不是 --dry-run。
    # 提前创建会让「无改动」和「预演」都在远端留下一个空分支。
    #
    # 但「有东西要推」之后到推送成功之间仍有很长的路：大文件预检、N 次建
    # blob、建 tree、建 commit、PATCH —— 任一步失败都会留下一个**空分支**，
    # 而本地状态文件的 tasks 里根本没有它（那要 PATCH 成功之后才写），
    # 用户只能靠 --prune 才发现。最典型的是改了个 12MB 文件被预检拒绝。
    #
    # 所以记住「本次是不是我们建的」，失败时补偿删掉。
    branch_created_now = None
    pushed_commit = None          # PATCH 成功前保持 None
    # N380（第一百二十一轮，低危）：复用已有分支时**不提示**会链式累积 commit。
    # `--hold` 的语义就是「保留分支反复推」，而每次推送独立建一个 commit、
    # parent 是上一次的 —— 实测连推 3 次 = 3 个 commit 进同一个 PR，
    # 正是使用者反映的「需 squash」。这是设计形态不是错误，但脚本
    # 在任何地方都没说过一句，用户只能事后从 PR 里发现。
    if wf == "pr" and not branch_missing and not dry:
        print(f"\n  · 复用已有分支 {branch}：本次会在其上**再建一个 commit**"
              f"（parent = 分支当前 head）。")
        print("     连推 N 次 = PR 里 N 个细碎 commit；要合成一个请"
              + ("新建分支（--new-task）" if _branch_from_hold
                 else "换一个分支名（--new-task 对显式 --branch 无效）")
              + "或合并时 squash。")
    if wf == "pr" and branch_missing:
        if dry:
            print(f"\n  · [dry-run] 将新建任务分支 {branch}"
                  f"（基点 = {BRANCH} {head_sha[:8]}），本次不创建")
            # N13：--dry-run 原先**完全不展示删除**，而删除是最不可逆的
            # 操作、恰恰是 dry-run 最该预览的内容。
            # 根因是 dry 早退发生在 plan_removals 之前，于是
            # `_report_removals(rm, dry_run=False)` 的 dry_run 恒为 False，
            # 模块内 dry_run=True 分支从未被执行过（死参数）。
            #
            # 这里在 dry 分支单独算一次并预览。不改动主路径的时序
            # （把删除计算整体提前会牵动熔断与确认的位置）。
            # N179：这里**不再调用**预览。
            # 本块（PR + 新分支 + dry）与下方通用 `if dry:` 出口是
            # **同一条执行路径**（中间没有 return），两处都调会打印两次。
            # 统一由下方出口调一次即可。
        else:
            # git 通道可用时**不预建分支**：git push 到 refs/heads/xxx
            # 会自己建，预建反而多一次 API 往返（且失败会留下空分支）。
            _use_git = _git_channel_ok()
            if _use_git:
                base_sha = head_sha
                target_ref = branch
                state.setdefault("tasks", {})[branch] = {"base_commit": head_sha}
                print(f"\n  · 走 git 通道推送（建分支 {branch}，"
                      f"基点 = {BRANCH} {head_sha[:8]}）")
            else:
                base_sha = create_branch(branch, head_sha)
                target_ref = branch
                branch_created_now = branch
                state.setdefault("tasks", {})[branch] = {"base_commit": head_sha}
                print(f"\n  · 新建任务分支 {branch}（基点 = {BRANCH} {head_sha[:8]}）")

    risky = []

    # -------- 失败即清理：以下任一步失败都不能留下僵尸分支 --------
    # 起点在**分支创建之后**尽可能早的位置，把第一层校验和 preview 也覆盖进来。
    # preview 要拉远端 blob 做 diff，网络一抖就抛错 —— 那时分支已建、
    # 却还没 PATCH，正是最容易留下僵尸分支的地方（实测）。
    # 本地补提交的结果，供成功输出使用。必须在 try 之前初始化：
    # 输出段在 try 之后，若推送中途抛异常根本走不到那里，但依赖「走不到」
    # 来保证名字存在是脆弱的隐式契约 —— 补一行赋值零成本。
    local_commit_state = "skipped"
    try:
        # -------- 第一层：文件级基线校验（mode + sha 一起比）--------
        # 判据已抽进 FileBaselineGuard（见文件顶部 Guard 段）。
        # 它同时是分流器：结果写回 ctx.todo / ctx.blocked / ctx.risky。
        ctx.files, ctx.lmap, ctx.rstate, ctx.risky = files, lmap, rstate, risky
        # N343：同 run_guards —— Guard 的输出整段保留
        with ui_keep_all():
            v = FileBaselineGuard().check(ctx)
        _enforce_audited(v, "file_baseline")
        todo, risky = ctx.todo, ctx.risky

        if not todo:
            # 同理：todo 为空但有删除时不能取消，否则纯删除永远推不出去。
            if not _pending_dels:
                # N459（第一百五十一轮，中危）：早退前先把基线跟上。
                #
                # 这批文件已被第一层判「本地 == 远端」而跳过，而 direct
                # 末尾那段 refreshed 刷新（"非本次推送…基线已跟进"）在
                # **推送之后** 才跑 —— 没有 todo 就永远到不了那里。
                # 于是基线里那份过期记录此后无人修正，**永久滞留**。
                #
                # 实测链条（两个 agent 改同一份内容，本仓库的常态）：
                #   先推的人 → 基线正常刷新
                #   后推的人 → 本地内容与远端一致 →
                #     推送   "没有需要推送的内容"
                #     --pull "没有需要合并的远端改动"
                #     --scope 归入 SAME
                #   三条主路径全说没事，而基线始终是旧值。
                #   唯一能修正的 --mark-synced，没有任何一处提示它。
                #
                # 方向是安全的（本地 == 远端，无覆盖/误删风险），
                # 危害是永久滞留 + 掩盖真实差异（--status/--scope 都
                # 看不出基线脏了，用户没有动机去修正）。
                #
                # 刷新判据与下面 refreshed 那段同源（lmap == rstate），
                # 不另起一套 —— 否则两处又会漂成不同口径。
                _n459 = []
                for _p, (_rm, _rs) in (rstate or {}).items():
                    if lmap.get(_p) != (_rm, _rs):
                        continue
                    _cur = state["files"].get(_p)
                    if not _baseline_stale(_cur, _rm, _rs):
                        continue
                    state["files"][_p] = {"mode": _rm, "sha": _rs}
                    _n459.append(_p)
                if _n459 and not dry:
                    # M119②：save_state 在 --dry-run 下是 no-op（N85，有意为之），
                    # 而这句「基线已跟进」原先**无条件**打印 —— 实测 dry-run
                    # 结束后基线仍是旧值，屏幕却说已跟进。与 N460 修掉的
                    # 「谎称已同步」同族，只是换了个出口。
                    save_state(state)
                    print(f"\n  · {len(_n459)} 个文件本地已与远端一致，"
                          f"基线已跟进（此前记录的 sha 已过期）")
                elif _n459 and dry:
                    print(f"\n  · [dry-run] {len(_n459)} 个文件本地已与远端一致，"
                          f"基线**未**跟进（预演不写状态文件）。")
                # M119①：无事可做 ≠ 取消。置位后两条取消通道都不置
                # _LAST_RUN_CANCELLED，退出码回到 0（CI 判成功）。
                global _NOTHING_TO_PUSH
                _NOTHING_TO_PUSH = True
                # M123（A114）：这条出口只 emit 了 events 块，没有结果 JSON。
                #
                # exit 都是 0、JSON 都能解析 —— 所以消费者不会报错，只是
                # 按契约读 `changed` / `count` 时拿不到，或拿到空。
                # 这比"没有 JSON"更难发现：一边是"解析失败"会被立刻察觉，
                # 另一边是"读到了但字段缺失"，看起来像"确实没改动"。
                #
                # 与上面 13619 那条「本地没有待推送的改动」保持同构
                # （同一组键、同一个 reason），否则两条"无事可做"出口
                # 在 --json 下又是两套口径。
                if _json_active():
                    print(json.dumps({
                        "ok": True, "reason": "nothing_to_push",
                        "action": "push",
                        "changed": [], "count": 0,
                        "pending_deletes": [], "pending_delete_count": 0,
                        # 这条出口独有的信息：刚被刷新的基线条数。
                        # dry-run 下 save_state 是 no-op（N85），落盘数为 0 ——
                        # 与屏幕那句「基线**未**跟进」保持同一口径。
                        "baseline_refreshed": 0 if dry else len(_n459),
                    }, ensure_ascii=False))
                    _mark_json_emitted()
                else:
                    print("\n没有需要推送的内容。")
                raise _Cancel()

        # 放行的必须显式再确认一次：--force-overwrite 是全局开关，
        # 一行参数就把「可能覆盖他人改动」的文件全放过去，代价太大。
        if risky and not yes:
            print(f"\n⚠️  以下 {len(risky)} 个文件将**整文件覆盖**远端内容"
                  f"（远端原版本不可恢复）"
                  + ("，本次是 --dry-run，**不会**真的覆盖）：" if dry
                     else "："))
            for rel in risky:
                print(f"   - {rel}")
            # N567（第一百八十五轮，低危）：`--dry-run` 明确声明"不做任何推送"，
            # 却在预览中途要求一次**破坏性确认** —— 无 TTY 时 `safe_input`
            # 按「否」处理 → 打印「已取消（未推送）」→ 退出码 4。
            #
            # 于是 M104 让 CI 预检拿到 0 的改善，**只在"没有放行文件"时成立**：
            # 一旦预检的改动里含放行文件（`--force-file` 场景恰恰是最需要
            # 预检的），又回到 4。实测：
            #   --dry-run --force-file a.txt        → 4（有「已取消」字样，自洽）
            #   --dry-run --force-file --yes a.txt  → 0
            #
            # dry 时只列出这批文件、不要求确认：确认是针对**真的要覆盖**的，
            # 而 dry-run 不会覆盖任何东西。清单照打 —— 预检的全部价值就是
            # 提前暴露这批文件，静音等于没预检。
            if dry:
                print("     （正式推送时会再次要求确认；"
                      "加 --yes 可跳过。）")
                ans = "y"
            else:
                ans = safe_input(
                    "确认用本地内容覆盖远端？输入 y 继续 (y/N) ").strip()
            if ans.lower() != "y":
                print("已取消（未推送）")
                raise _Cancel()

        # -------- 远端已前进的整体提示 --------
        prev = state.get("base_commit")
        if prev and prev != head_sha:
            print(f"\n⚠️  远端自上次推送后已前进（基线 {prev[:8]} → 现在 {head_sha[:8]}），可能含他人提交。")
            print("   本次只覆盖下面校验通过的文件，其余文件保持远端原样。")

        # -------- 预览 --------
        # M201（A169，中危）：`preview()` 的函数体里 `_json_active` /
        # `_stream_active` **零出现**，7 个打印点全是裸 print。于是
        # `--json` 下它的整段人类可读输出（"待推送 N 个文件…"、逐文件
        # `M a.txt +1 / -1`）落在 JSON **之前**，实测 json.loads 报
        # "Expecting value: line 1 column 3"。
        #
        # 而脚本自己早在 2091 行就记录过这个失败模式（`--status --json`
        # 下 git() 的一行告警会让整体不可解析），并在 2101 / 9774 两处用
        # `redirect_stdout` 收进 warnings —— 唯独预览这条没接。
        #
        # 它不是"文档已声明不支持"：推送路径自己一直在造 JSON（无事可做
        # 那条出口有 json 分支，注释还写着要与另一条出口"保持同构"），
        # 且**恰恰在有改动时才失效** —— 空提交测试通过、上了真实改动才炸，
        # 而 M86 点名「CI 最常见：`--json -m x --yes`」正是这个场景。
        #
        # 收进 buffer：json 模式并进 events（机器拿得到），--mine 落到
        # stderr（stdout 是纯数据流，但信息不丢）。
        if _stream_active():
            _pv_buf = io.StringIO()
            with contextlib.redirect_stdout(_pv_buf):
                preview(todo, rstate, lmap)
            _pv_txt = _pv_buf.getvalue().rstrip("\n")
            if _pv_txt:
                if _json_active():
                    _pv_ui = get_ui()
                    if _pv_ui is not None:
                        _pv_ui.line(_pv_txt, "info")
                    else:
                        sys.stderr.write(_pv_txt + "\n")
                else:
                    sys.stderr.write(_pv_txt + "\n")
        else:
            preview(todo, rstate, lmap)

        # -------- 大文件预检 --------
        # 判据已抽进 SizeGuard（见文件顶部 Guard 段）。
        ctx.todo = todo
        # N407 复核：StaleLocalGuard 因"纯新增"放行后，实际要推的文件
        # 必须**仍然全部**是远端不存在的。点名范围可能被绕过（N370：
        # 点名范围外的删除被无条件追加回来），所以这里按事实再查一次。
        if getattr(ctx, "p0_new_only", False):
            _in_r = [p for p in todo if p in (rstate or {})]
            if _in_r:
                _blocked(
                    "本次按「纯新增」放行了本地副本过期的检查，但最终文件集合里"
                    f"有 {len(_in_r)} 个**远端已存在**的文件："
                    f"{_in_r[:5]}{' …' if len(_in_r) > 5 else ''}\n"
                    "  这些文件会被本地旧副本整文件覆盖（静默回退远端更新）。\n"
                    "  先跑 --pull 合入远端改动，或只点名远端不存在的新文件。")

        # N343：同上
        with ui_keep_all():
            # M169（A153，低危）：tag 原先是手写的 "size_guard"，而
            # `SizeGuard.id = "oversize_blob"`。实测审计原文里是
            # `"guard": "size_guard"`，JSON events 里是 `"guard":
            # "oversize_blob"` —— 拿 JSON 里的名字去审计里检索，一条
            # 都查不到。
            # 对照：FileBaselineGuard 那一处传的正是 `file_baseline`
            # （与它的 id 一致），所以这不是「tag 与 id 本就是两套命名」
            # 的约定，而是这一处写错了。
            _enforce_audited(SizeGuard().check(ctx), SizeGuard.id)

        # -------- 提交信息 --------
        # 原来复用 `git log -1`，但推送成功后本脚本会在本地补一个同信息的 commit，
        # 导致从第二次起 log -1 永远拿到同一条 → 所有推送共用同一个提交信息。
        msg = opts["msg"]
        if msg is None and not yes:
            # N311：提交信息是**录入型**（有合法空值——留空用默认文案），
            # 但"非交互"的原因不是"用户选了否"，而是拿不到输入。
            # 留空回落默认文案本身是对的，只是提示文案要分开说。
            msg = safe_input(
                f"提交信息（单行，留空则用「{default_msg(todo)}」）：\n> ",
                kind="entry").strip()
        if not msg:
            msg = default_msg(todo)
        if not opts["msg"]:
            print(f"提交信息：{msg}")

        if dry:
            # N165：dry-run 必须**先预览**，再说"未做任何推送"。
            # 原先直接 raise _Cancel()，删除预览在 direct 工作流下
            # 从未执行 —— 而删除是最不可逆、最该先看一眼的操作。
            # N182：此处 todo / extra 都已就绪，却仍传候选集 files。
            # files 含最终会被拦下或屏蔽的路径，于是预览比正式**偏乐观**
            #（重命名配得更多 → 熔断更不容易触发）。
            # 预览的价值在"结论与真跑一致"，偏乐观比偏保守更危险。
            _dry_preview_removals(state, head_sha, ignored_files,
                                  sorted(set(todo) | set(extra or [])),
                                  opts, _pending_dels,
                                  # M83：与正式执行同源 ——
                                  # explicit 为空时一律 None（不过滤），
                                  # 非点名推送行为不变。
                                  allowed=({_r for _r in
                                           (safe_rel(_f) for _f in explicit)
                                           if _r} if explicit else None),
                                  # N521：传本次的 lmap，供非 UTF-8 计数
                                  # （sync_local_seen 此时还没跑）。
                                  lmap=lmap)
            # N573（第一百八十七轮，低危）：A127 只论证了 `refreshed`
            # 那句在 dry 下不可达，**lagging 那条同样不可达** —— 而它
            # 不是"信息"，是一条**安全告知**：原文写的是「推它们会覆盖
            # 远端」。预演的意义正是"推之前先看一眼"（脚本自己在 N165
            # 那处就是这么论证的，为此把删除预览提到 `raise _Cancel()`
            # 之前），而最该在预演里看到的覆盖警告恰恰在预演里没有。
            #
            # 与早退路径那处 M119② 的对照是现成的：同一个「基线刷新」
            # 语义、同一个 `_main()`，那里 dry 与非 dry **成对**写全了。
            #
            # 用 `_classify_remote_drift`（只读）而不是搬正式刷新那段：
            # 预演不写状态文件（N85），这里只分类、不改 state。
            _dr, _dn, _lag_dry = _classify_remote_drift(
                state, lmap, rstate, todo)
            if _lag_dry:
                print(f"\n⚠️  [dry-run] {len(_lag_dry)} 个文件本地落后于远端"
                      f"（**推它们会覆盖远端**，本次预演不会刷新它们的基线）：")
                for _p in _lag_dry[:20]:
                    print(f"   - {_p}")
                if len(_lag_dry) > 20:
                    print(f"   … 其余 {len(_lag_dry) - 20} 个")
                print("   改这些文件前请先：python3 push_api.py --pull")
            print("\n[dry-run] 未做任何推送")
            # M104：仍走 _Cancel 清理通道（与失败同一条），但要标出
            # "这次是 dry-run"，让下面的置位跳过 —— 见 _DRY_RUN_ONLY 注释。
            global _DRY_RUN_ONLY
            _DRY_RUN_ONLY = True
            raise _Cancel()
        if not yes:
            ans = safe_input(f"\n推送到 {OWNER}/{REPO}@{target_ref}？(y/N) ").strip().lower()
            if ans != "y":
                print("已取消")
                raise _Cancel()

        # -------- 第二层：ref 乐观锁（分两遍，各司其职）--------
        #
        # 只查一遍是不够的：建对象（尤其大文件建 blob）可能耗时几十秒，
        # 那才是真正的并发窗口。若锁只放在建对象**之前**，窗口完全没盖住 ——
        # 改动虽仍不丢（PATCH force=False 会挡住非快进，fail-safe），
        # 但报错变成 GitHub 的 422 原文 "not fast-forward"，
        # 用户只会往权限/设置上猜，不知道该去 --pull。
        #
        # 第一遍（建对象前）：早失败，省掉几十秒的大文件上传。
        #   新分支尚未创建时 target_ref 不存在，此时要查主干 ——
        #   新分支的基点就是主干，要确认的正是「主干没前进」。
        lock_ref = BRANCH if branch_missing else target_ref
        _recheck_ref(lock_ref, base_sha, "开始建对象前")

        # -------- N37：删除熔断**提前**到建 blob 之前 --------
        # 原先顺序是「先上传全部 blob → 再算删除 → 再熔断」。
        # 熔断的设计目的是拦误判（git 读取失败 / 切错 --root / 用错基线），
        # 而误判场景下判定发生时已经上传了全部待推文件（大文件可达数十 MB、
        # 数分钟），然后才打印「本次未推送任何内容」——
        # 措辞没错，但用户已经等了很久，会以为工具卡住或已经推了。
        #
        # 提前的代价：需要远端状态（算删除要用 rstate），而 rstate 在此前
        # 通常已由第一层校验拉过（走进程内缓存），额外成本接近 0。
        # 拿不到时跳过预检，不阻断主流程 —— 预检只省时间，不参与判据。
        if (_plan_removals is not None and not _deletes_disabled()
                and _pending_dels):
            try:
                # N114：这里**漏传 will_push** —— 而正式路径（8142 附近）
                # 是传的。于是预览与正式路径用不同参数算出不同结果：
                # 实测整仓改名时，预览把重命名全判成真删除 → 误报熔断；
                # 正式路径却正常放行。同一个操作，预演说不行、真跑可以。
                #
                # 预演的价值全在"结论与真跑一致"，参数必须同源。
                _rm_pre = _plan_removals(
                    state, remote_state_map(head_sha), local_state_map(),
                    deleted=_pending_dels,
                    exists=_local_exists,
                    # N268（第七十八轮，中危）：三个调用点里**只有这里
                    # 不传 force**，而另两处（dry 预览、正式执行）都传了
                    # —— force 是唯一"两处传、一处不传"的参数。
                    #
                    # N597（A156，低）：**本注释同样已过时** —— 三处现在
                    # 全部传了。194 轮报的正是这一处，而它审的代码里这一行
                    # 明明白白传着 force。与上面 5805 那处是同一族的两例。
                    # 不删原文，只标注，理由见 5805 那处。
                    #
                    # 实测（--force-delete 场景）：
                    #   force=False  entries=['gone.txt']
                    #                skipped=[('remote_only.txt', '本地从未拥有过…不删')]
                    #   force=True   entries=['gone.txt', 'remote_only.txt']  ← 多删一个
                    #               skipped=[]
                    #
                    # 后果两条：
                    #  ① 预检漏报一个真正会删的路径，而漏的**正是
                    #     --force-delete 唯一改变的那一类**；
                    #  ② 数量低估 → 本可在上传前触发的熔断不触发 →
                    #     N37「上传前提前熔断」的收益在 --force-delete
                    #     下归零。
                    force=_deletes_forced(opts),
                    gitignored=(lambda p: p in ignored_files),
                    # 这两个在正式路径里于建 tree 之前才算（那里才有 todo/extra）。
                    # 预览发生在更早的位置，必须**就地按同样口径算一遍**：
                    # 直接引用后面的变量名会 NameError，被 except 吞掉后
                    # 预览静默失效 —— 比漏传更难发现。
                    submodules={p for p, (m, _s) in
                                (state.get("files") or {}).items()
                                if _is_submodule_path(p)},
                    # N138：N114 补传了 will_push，但口径用了 `files`
                    #（候选集）而非 `todo`（过完 Guard 后真正会推的）。
                    # 二者不等：files 里含被拦下 / 被 gitignore 屏蔽 /
                    # 不存在的路径。于是预览把「其实不会推上去」的新路径
                    # 也当成"内容有了新去处"，配出不该成立的重命名，
                    # 与正式路径结论不同 —— 预演就失去意义了。
                    #
                    # todo 在此处**已就绪**（上面 FileBaselineGuard 之后
                    # 就已赋值），不存在引用过早的问题。
                    will_push=set(todo) | set(extra or []),
                    # M83（A86，P2）：与正式执行、dry 预览同源。
                    # 预检是 N37「上传前提前熔断」的**唯一依据**：
                    # 不传 allowed 会让删除条目虚高（点名范围外的被算进来），
                    # 于是**本不该熔断时拦下用户**。
                    #
                    # 这与 N268 当时的方向相反：N268 是漏报、低估
                    #（该熔断没熔断），本处是高估（不该熔断却熔断）。
                    # 同一个参数、同一处遗漏，两个方向的错都会发生。
                    allowed=({_r for _r in (safe_rel(_f) for _f in explicit) if _r}
                                  if explicit else None))
                if _rm_pre.get("abort"):
                    _blocked(
                        f"\n删除熔断，本次未推送任何内容"
                        f"（改动都还在本地，且**尚未上传任何文件**）。\n"
                        f"   {_rm_pre['abort_reason']}")
            except SystemExit:
                raise
            except Exception as _e:
                print(f"  ! 删除预检失败，改为推送后再判："
                      f"{type(_e).__name__}: {_e}")

        # -------- 建对象 --------
        entries = []
        for rel in todo:
            mode = lmap.get(rel, ("100644", ""))[0]
            full = os.path.join(ROOT, rel)
            if mode == "120000":                      # symlink：blob 内容是目标路径
                raw = os.readlink(full).encode("utf-8", "surrogateescape")
            else:
                with open(full, "rb") as f:
                    raw = f.read()
            blob = api("POST", "/git/blobs",
                       {"content": base64.b64encode(raw).decode(), "encoding": "base64"},
                       timeout=_timeout_for(len(raw)))
            if "sha" not in blob:
                # 整批失败时的**可恢复性**必须说清楚，否则用户不敢重跑。
                #
                # 这里是逐个建 blob、任一失败即整批中止（tree / commit
                # 都还没建）。看上去"前 K 个白做了"，其实不会留下任何
                # 远端痕迹：
                #   · blob 是**内容寻址**的（见 _retryable 的说明），
                #     重发得到同一个 sha，天然幂等 —— 重跑不会重复创建，
                #     也不会产生重复的远端对象；
                #   · tree / commit / PATCH ref 一步都没走，远端**一个字节
                #     没变**，分支与主干都还是原样；
                #   · 本地基线也没写（save_state 在推送成功之后）。
                #
                # 所以正确动作就是原样重跑。不说这句，用户看到"失败"会
                # 以为状态脏了，转而去用 --force-overwrite 之类的手段
                # "清理"—— 那才是真会出事的一步。
                #
                # 弱网下这类失败并不罕见（外部实测反馈：批量越大越容易
                # 整批失败，单次成功率偏低）。想降低重跑成本，可以先
                # `--scope --mine` 拿到清单再逐个点名推送，而不是一次
                # 带上全部文件 —— 点名推送把"整批"拆成"单个"，
                # 失败只影响那一个。
                raise SystemExit(
                    f"创建 blob 失败 {rel}: {json.dumps(blob)[:300]}\n"
                    f"   已建的 {len(entries)} 个 blob 是幂等的（内容寻址），"
                    f"重跑不会重复创建；\n"
                    f"   tree / commit / ref 均未提交，远端未改动，"
                    f"本地基线也未写 —— 原样重跑即可。\n"
                    f"   批量越大越容易卡在这步；可用 "
                    f"`--scope --mine` 取得清单后逐个推送。")
            # mode 必须跟着走，否则 .sh / 二进制推上去就丢了可执行位。
            entries.append({"path": rel, "mode": mode, "type": "blob", "sha": blob["sha"]})
            print(f"  blob {rel} ({mode})")

        # -------- 删除 / 重命名 --------
        # 建 tree **之前**决定，因为删除条目（sha: null）要一起进 entries。
        #
        # 判据不靠 git status 的 "D"：推送成功后脚本会补一个 commit，
        # "D" 随之消失，删除信号丢失（实测）。改用「基线有 & 本地无 &
        # 本地曾经有」三信号交集，与 commit 状态无关。
        _rm_paths = []                # 本次实际删除的路径（N55 收尾要用）
        if _plan_removals and not _deletes_disabled():
            # N247：这里的口径是「基线全量里被 .gitignore 屏蔽的」，
            # 而 dry 预览（2867）与熔断预检（9345）用的是
            # `ignored_files`（待推送集合 files 里被屏蔽的）。
            # 三处口径不同源：
            #   · 预览/预检：只覆盖 files → 被屏蔽路径**放行**
            #   · 正式执行：覆盖基线全量 → 被屏蔽路径**静默过滤**
            # 于是预览说"会删"，正式执行却悄悄不删（或反之）。
            #
            # 统一到待推送集合 + 删除候选（即本次真正会动的范围），
            # 与预览/预检同源。
            #
            # N247 的第三半：`_dels_ignored`（被 .gitignore 屏蔽、
            # 已从 _pending_dels 里滤掉的删除候选）必须并进来。
            # 它们已不在 _pending_dels 里，光算 `files | _pending_dels`
            # 会漏掉 —— 而模块内部会自行把 committed 那一路加回来，
            # 于是结果是「门控说不算删除、模块照样删」。
            # 三处口径 = files ∪ lmap ∪ _dels_ignored（预览/预检侧
            # 由上面的 ignored_files |= _dels_ignored 补齐）。
            # M187：同上，裸调 → 异常穿透到顶层。
            ignored = _gitignored_safe(
                set(files) | set(_pending_dels or []))[0] | set(_dels_ignored)
            # will_push：本次真的会推上去的路径（todo + 分支上要补齐的）。
            # 重命名配对必须看它 —— 只在本地存在、实际不会被推送的新路径
            # 不能算「内容有了新去处」，否则「删旧 + 新内容没上去」会被
            # 当成重命名放行，内容真的丢了（H1）。
            _will_push = set(todo) | set(extra or [])
            # submodules：tree API 里 type=commit，被 remote_state_map 的
            # blob 过滤挡掉，rstate 里取不到 → 「远端被他人改动」闸门对它们
            # 恒失效（N1）。必须显式告知模块。
            _subs = {p for p, (m, _s) in (state.get("files") or {}).items()
                     if _is_submodule_path(p)}
            rm = _plan_removals(
                state, rstate, lmap,
                # N159（**已修**，此处改过去时）：这里**曾经**传的是
                # `deleted=_detect_deletions()` —— 原始、未过滤的集合。
                # 而 N49 的点名约束（点名范围外的删除本次不处理）
                # 只过滤了门控用的 _pending_dels，没进执行。
                # 于是门控打印「本次不处理，它们仍留在远端」，
                # 执行却照样把它们删了 —— 声明与行为相反，
                # 且删的是用户明确要求保留的东西。
                #
                # 现状：传的是 `_pending_dels`（已过滤），下面 N370
                # 又补了 `allowed` —— **两处都改完了**。
                #
                # M84（A86，P3）：这条注释在 N159 修好后**没跟着改**，
                # 仍用现在时描述一个已修的缺陷，于是与紧邻下方 N370
                # 那条自相矛盾：后者说"只过滤 _pending_dels 不够"，
                # 前者却说传的是未过滤集合。
                # 与 M42（N240 注释的前提被后来的修复改变）同族 ——
                # 只是这里连语气都没换成过去时。
                deleted=_pending_dels,
                # N11：删除的放行用独立开关，不与 force 混用。
                force=_deletes_forced(opts),
                gitignored=(lambda p: p in ignored),
                exists=_local_exists,
                submodules=_subs,
                will_push=_will_push,
                # N370：把点名范围传进模块。只过滤主流程的 _pending_dels
                # 不够 —— 模块内部会把 committed 补回来。
                allowed=({_r for _r in (safe_rel(_f) for _f in explicit) if _r}
                                  if explicit else None),
            )
            # N521（第一百七十二轮，中危）：M44 给 `skipped_non_utf8`
            # 接上了数据源，但**顺序接反了** —— 报告先跑、数据源后填充。
            # 实测（打桩记录调用顺序与传入值）：
            #   0  ('report_removals', 'skipped_non_utf8=0')   ← 先
            #   1  ('sync_local_seen', 'dropped=1')            ← 后
            # `_main` 里唯一会调 sync_local_seen 的两处都在本行**之后**，
            # 而其余调用点分属 init_baseline / mark_synced / pull /
            # resolve / _refresh_main_baseline —— 都不在推送路径上。
            # CLI 一次运行一个进程、reset_per_run_globals 又会在开头清成
            # 空集，所以**没有任何一条真实路径能让它非零**：
            # 线接上了，两端却不在同一次运行里同时有值。
            #
            # 于是 M44 注释里要消除的那件事照旧发生、照旧无声：这些路径
            # 永久进不了 local_seen，日后删除会被判成「远端独有」而静默
            # 跳过 —— 而「静默丢弃正是删除功能最该说清楚的事」。
            #
            # 此处**当场**按 lmap 统计，而不是提前调 sync_local_seen：
            # 后者会修 state["local_seen"] 并打印「裁掉了 N 条」，在本行
            # 之前跑一遍等于把那条提示重复打一次；而这里要的只是"有几个
            # 路径被挡在外面"这个数。
            _n_utf8 = len(_NON_UTF8_DROPPED)
            if not _n_utf8:
                _n_utf8 = sum(1 for _p in lmap if not _encodable(_p))
            # N489（A121，中危）：删掉旧路径之前，先把「内容在本地有
            # 新位置、但那个位置**不会上传**」点名。
            #
            # 不点名的后果是内容从远端彻底消失且无人察觉：报告只说
            # 「将删除 1 个远端文件：old.txt」—— 那句话是真的，但没说
            # 「这份内容就此没有远端副本了」。本地虽然还在（只是被
            # .gitignore 屏蔽），远端一份都不剩。
            for _old, _new in (rm.get("shadowed") or []):
                # N598（A155，中）：`_new` 现在可能是含 surrogate 的
                # 真实路径（不可编码的新去处），裸打会崩 —— 显示一律
                # 走 `_disp_path`，并在真发生了转义时说明是转义显示
                # （N600）。
                _dn = _disp_path(_new)
                _esc = "（此处为转义显示）" if _dn != _new else ""
                print(f"  ⚠️  {_disp_path(_old)} → {_dn}{_esc}：新位置被 "
                      f".gitignore 屏蔽（或无法编码），**不会上传** —— "
                      f"删除后这份内容在远端将没有副本，"
                      f"请确认无误或取消屏蔽。")
            # N105：`ui=locals().get("ui")` 恒为 None ——
            # locals() 在函数体内只含**局部变量**，而 ui 在这里根本不是
            # 局部变量（它要么不存在、要么是全局），于是永远拿不到。
            # 后果：删除报告走的是降级 print 分支，
            # --json 模式下这些行收集不到，结构化输出覆盖不到删除路径。
            # 用 get_ui() 取真正的 Ui 实例（没有时返回 None，行为不变）。
            _report_removals(rm, dry_run=False, ui=get_ui(),
                             skipped_non_utf8=_n_utf8)
            # N9：删除是脚本里**唯一不可逆**的远端操作，却是唯一在
            # 用户授权之后才被告知的操作。
            # 时序：用户在建 blob 之前按了 y（那时还不知道会删什么），
            #      blob 传完 → 这里才算出并打印「将删除 N 个」→ 直接建 tree。
            # 新增/修改都会先经 preview/--status 呈现，删除没有。
            # --yes 下从授权到建 tree 全程无人看过那句「不可逆」。
            #
            # 所以在这里补一次**针对删除**的独立确认。默认开启；
            # 只有 --yes 跳过（此时用户已明确放弃交互，与新增/修改一致）。
            _rm_now = [e["path"] for e in (rm.get("entries") or [])]
            if _rm_now and not yes and not dry:
                _ans = safe_input(
                    f"确认删除上述 {len(_rm_now)} 个远端文件？"
                    f"此操作不可逆，输入 y 继续 (y/N) ").strip()
                if _ans.lower() != "y":
                    print("已取消（未推送，也未删除任何远端文件）")
                    raise _Cancel()
            elif _rm_now and yes:
                # --yes 下至少让它显眼：混在大段输出里会被漏看
                print(f"  ⚠️  --yes 已跳过删除确认："
                      f"本次将删除 {len(_rm_now)} 个远端文件（不可逆）")
            if rm["abort"]:
                # abort 时模块已清空 entries，这里再兜一道：
                # 删除不可逆，任何一条出口都不能带着删除条目往下走。
                _blocked(
                    f"\n删除熔断，本次未推送任何内容（改动都还在本地）。\n"
                    f"   {rm['abort_reason']}"
                )
            entries.extend(rm["entries"])
            _rm_paths = [e["path"] for e in rm["entries"]]
            # N382：这里才算"执行侧"—— 条目已进入本次要建的 tree。
            # 登记侧（_RUN_PENDING_DELETES）在推送前就有值，
            # 若删除被熔断/取消，它仍有值而实际一个没删；
            # 只有走到这里才能断言"送出了"。
            global _RUN_DELETED_PATHS
            _RUN_DELETED_PATHS = sorted(
                set(_RUN_DELETED_PATHS or []) | set(_rm_paths))
            if _rm_paths:
                # 删掉的路径要从基线里摘掉：留在基线里的话，下次它仍会被
                # 当成「远端有、本地没有」而再次进入待处理集合。
                #
                # 但**何时**摘取决于工作流（N2）：
                #   direct —— 紧接着就 PATCH 主干，删除立即生效，现在摘是对的
                #   pr     —— 主干此刻**还没删**，文件还好好的在上面。
                #            现在摘等于让基线谎报「主干上没有这个文件」，
                #            等 PR 期间跑 --pull，pull 会把它当成「远端新增」
                #            写回本地 —— 用户刚删的文件回来了，日志还说是
                #            「远端新增」（实测复现，且无需任何失败步骤）。
                #            必须等 merge 成功、主干真的删了再摘。
                if wf == "pr":
                    state.setdefault("pending_removals", [])
                    state["pending_removals"] = sorted(
                        set(state["pending_removals"]) | set(_rm_paths))
                    print(f"\n  · {len(_rm_paths)} 个删除将随 PR 合并生效；"
                          f"合并前请勿 --pull，否则它们会被当成"
                          f"「远端新增」拉回本地。")
                else:
                    for _p in _rm_paths:
                        state["files"].pop(_p, None)
        elif _plan_removals:
            print("\n  · 删除功能已关闭（默认），远端删除的文件将保留。")
            # N50：纯删除场景会**逐个列出**文件名（让用户判断「这几个
            # 我是真想删吗」），而最常见的混合场景（改几个文件同时删一个）
            # 只有上面这句泛泛说明 —— 用户看不到是哪些文件被留在了远端。
            # 逐条列出的目的在混合场景下完全落空，这里补上。
            if _dels_all:
                print(f"     被留在远端的 {len(_dels_all)} 个已删除文件：")
                for _p in _dels_all[:20]:
                    print(f"        - {_p}")
                if len(_dels_all) > 20:
                    print(f"        … 其余 {len(_dels_all) - 20} 个")
            print("     开启方式：" + _delete_how_to_enable().split(chr(10))[0])
            _print_how_to_enable(6)
        elif _PLUGIN_ERROR:
            # N53：删除不可用时原先**一个字都不输出**。
            # 分支矩阵里 (None, 未禁用) 这一格两个分支都不命中，
            # 于是用户看到「有 3 个删除待落实」然后「✅ 已推送」，
            # 而实际上一个都没删，也没有任何解释。
            #
            # 危险在于这是**能力消失但契约还在**：文档把
            # PUSH_API_ENABLE_DELETE=1 描述为「常开场景」，用户依赖它
            # 自动清理远端；模块一缺失，所有删除静默失效，而用户会一直
            # 以为在用。典型触发：只部署了 push_api.py 单文件。
            #
            # ⚠ 2026-09-23 修正：原文案写的是「删除功能已开启，但删除模块
            # 不可用」，与下一行「原因：默认关闭，删除不可逆」**自相矛盾**
            # —— 实测每次默认推送都会打出这两行。
            #
            # 根因是 N53 当年的假设已经过期。它写于插件还是外部文件时，
            # 那时 `_PLUGIN_ERROR` 的成因是「用户开了删除、模块却加载失败」。
            # 插件合并进单文件后（见 `_bind_removals`），`_PLUGIN_ERROR`
            # **只有一处赋值来源**、且只在 `_deletes_disabled()` 为真时：
            #     _PLUGIN_ERROR = "默认关闭，删除不可逆"
            # 也就是说：`_PLUGIN_ERROR` 非空 ⟺ 删除功能**未启用**。
            # 这个分支里删除永远不可能「已开启」。
            #
            # 后果不只是难看：用户看到「已开启」会去查环境变量、找模块，
            # 而真因是默认关闭 —— 排查方向被指向了相反的一侧。
            # 与 N159（门控声明与执行行为相反）是同一类。
            #
            # 措辞改为与 `_plugin_note()` 一致（那里写的是
            # 「删除/重命名支持未启用（{原因}）」），不再断言"已开启"。
            print("\n  ! 删除/重命名支持未启用，本次**不会删除任何远端文件**。")
            print(f"     原因：{_PLUGIN_ERROR}")
            print("     开启方式：" + _delete_how_to_enable().split(chr(10))[0])
            _print_how_to_enable(6)
            if _pending_dels:
                print(f"     本次有 {len(_pending_dels)} 个删除待落实，"
                      f"已全部跳过：")
                for _p in _pending_dels[:10]:
                    print(f"        - {_p}")
                if len(_pending_dels) > 10:
                    print(f"        … 其余 {len(_pending_dels) - 10} 个")


        # N397（第一百二十九轮）：让"静默覆盖"变成可见。
        if REMOVAL_WARN_LINES > 0 and head_sha:
            try:
                _rmap = remote_state_map(head_sha) or {}
                _todo = list(todo or [])
                _rep = _content_removal_report(_rmap, _todo)
                if _rep and _rep[0] >= REMOVAL_WARN_LINES:
                    print(f"\n  ⚠️  本次将从远端移除约 {_rep[0]} 行内容"
                          f"（不是文件删除，是这些文件里**已有的行**消失）：")
                    for _r, _ln in _rep[1][:12]:
                        print(f"     - {_r}: {_ln.strip()[:90]}")
                    if len(_rep[1]) > 12:
                        print(f"       … 其余 {len(_rep[1]) - 12} 行")
                    print("     若这些改动来自**别人的推送**（并发编辑），"
                          "说明本次覆盖了对方的成果 —— 请先 --pull 核对。")
                    if len(_todo) > REMOVAL_CHECK_CAP > 0:
                        print(f"     （只对前 {REMOVAL_CHECK_CAP} 个文件"
                              f"做了逐行比对，共 {len(_todo)} 个；"
                              f"每个文件一次请求，全查会撞限流 ——"
                              f"可用 PUSH_API_REMOVAL_CAP 调整）")
            except BaseException:
                pass

        # N17：删除**全部被跳过**时不能照样建提交。
        # 实测：所有删除因「本地仍存在」/「远端已被他人改动」被 skip，
        # entries 为空 → 用 {"base_tree": base_sha, "tree": []} 建 tree
        # → 树与 base 完全相同 → 建 commit → PATCH → 开 PR → 自动合并。
        # 净效果：远端多一个**空提交**，PR 工作流下还多一轮空 PR。
        # 而用户看到的是「· a.txt 未删除：…」紧接着「✅ 已推送」——
        # 「没删成」和「推送成功」同时出现，极易误解为删除已生效。
        if not entries and not todo:
            print("\n  · 本次实际没有任何远端变更（删除全部被跳过，"
                  "且无文件内容改动），不创建提交。")
            print("     跳过原因见上方「未删除」清单。")
            if branch_created_now:
                _cleanup_failed_branch(branch_created_now, head_sha,
                                       pushed_commit)
            return

        # -------- git 通道：先试，成功就不再走 API --------
        #
        # N216（阻断级）：上一轮为修 N207 删掉了旧的调用块，新块却因脚本
        # 在写文件前抛异常而**从未落盘** —— git_send 变成**零调用点**。
        # git 通道启用时 blob/tree/commit/push/PATCH 五个动作全部跳过：
        # 远端零改动，随后 pushed_commit[:8] 抛 TypeError。
        #
        # 位置：必须在删除规划（算出 _rm_paths）**之后**、建 tree 之前。
        #   · 太早 → _rm_paths 尚未定义（NameError）
        #   · 用 N193 那种"循环里 continue"的写法 → git 失败回退时
        #     entries 已空，全部文件内容静默丢失（N207）
        # 现在 blob 循环无条件执行，entries 始终完整，回退必然安全。
        #
        # 代价：git 通道下仍会传一遍 blob（N193 的省请求收益暂不生效）。
        # 正确性优先于优化 —— 等 _rm_paths 的算序调整后再考虑。
        if _use_git and not dry:
            try:
                pushed_commit = git_send(target_ref, base_sha, todo,
                                         _rm_paths, msg, lmap)
                # N202：收尾共 10 处引用 commit["sha"]，唯一赋值点却在
                # API 分支里 → git 成功时 NameError，且落在主 try 之外
                #（不清理、不落盘、无失败审计）。这里补同形状的值。
                commit = {"sha": pushed_commit}
                print(f"  · git 通道推送完成 {pushed_commit[:8]}")
            except SystemExit as _ge:
                # 探测说通、实际 push 撞墙（认证过期、权限不足）时
                # 不能把改动丢在这里 —— 回退 API 再试一次。
                # M57（P1）：_ge 里带着「git push {url} 失败」，而 url
                # 可能内嵌 token。**git 通道一旦失败就必发生** —— 这是
                # 三条出路里最常被打出去的一条。
                print(f"  ! git 通道推送失败，回退 API："
                      f"{_scrub_url(str(_ge))}")
                _RT.no_git = True
                _use_git = False

        if not _use_git or dry:
            tree = api("POST", "/git/trees", {"base_tree": base_sha, "tree": entries})
            if "sha" not in tree:
                raise SystemExit(f"创建 tree 失败: {json.dumps(tree)[:300]}")

            # N378：parents 必须是可信 sha，否则服务端回 422 且原因看不懂。
            # （空仓库"没有 parent"是另一条路，见下面 N379 的条件表达式。）
            if base_sha and not _is_sha(base_sha):
                raise SystemExit(
                    f"取到的远端 head 不是合法 sha：{base_sha!r}\n"
                    f"   它会被原样塞进 POST /git/commits 的 parents，"
                    f"服务端会回 422 且看不出原因。\n"
                    f"   常见成因：代理改写了响应、非标准 Git 服务、"
                    f"或 ref 接口返回了短 sha。")
            # N379（第一百二十一轮，中危）：parents 原先**无条件**
            # `[base_sha]`。空仓库（还没有第一次 commit）里 base_sha 是
            # None → 传成 `[null]`，服务端不会提示"要建根 commit"，只回
            # 一个看不懂的错误。GitHub 的约定是明确的：空仓库要建
            # **根 commit**，parents 传空数组 []。
            commit = api("POST", "/git/commits", {
                "message": msg,
                "tree": tree["sha"],
                "parents": [base_sha] if base_sha else [],
            })
            pushed_commit = commit.get("sha")
            if "sha" not in commit:
                raise SystemExit(f"创建 commit 失败: {json.dumps(commit)[:300]}")

        if _use_git and not dry:
            # git 通道已经在 git_send 里 push 过了（非快进由远端拒绝，
            # 与这里 force=False 同一语义），不需要再 PATCH ref。
            pass
        else:
            # -------- 第三层：PATCH 用 force=False，非快进由 GitHub 拒绝 --------
            # 第二遍 ref 检查：覆盖建对象期间的并发窗口（真正的窗口在这里）。
            _recheck_ref(target_ref, base_sha, "建对象完成后")

            upd = api("PATCH", _ref_path(target_ref), {"sha": commit["sha"], "force": False})
            if not isinstance(upd, dict) or "object" not in upd:
                # 结构不符 ≠ 推送失败。此时 PATCH 已返回 2xx，分支很可能已经
                # 更新成功，只是响应格式与预期不同（GitHub 改结构、代理改写等）。
                # 直接报「失败」会让用户在推送其实已生效的情况下重推。
                print("  ! 无法确认 ref 更新结果（响应结构异常），"
                      "请用 --status 核实远端状态后再决定是否重推。")
            elif upd.get("object", {}).get("sha") != commit["sha"]:
                raise SystemExit(f"更新 ref 失败（可能非快进）: {json.dumps(upd, ensure_ascii=False)[:300]}")

        # N383：从这一行起，**远端已经有本次全部内容**。
        # （git 通道在 git_send 的 push 里完成，API 通道在上面的 PATCH。）
        #
        # 而下面「失败即清理」的 try 在本地补提交结束后就合上了，
        # 之后的 save_state / ensure_pr / do_merge 全在 try 之外 ——
        # 那三步里任何一步抛出非 SystemExit 的异常（OSError 最常见：
        # 磁盘满 / 只读挂载），都会同时发生三件事：零审计、
        # 零清理、裸 traceback。记下 ref，让 _main_wrapped 的兜底
        # 至少能说出「内容已经推到哪个 ref」。
        #
        # N384：用 target_ref 而不是 branch_created_now —— 后者只在
        # API 通道那一支赋值，git 通道下恒 None，于是 audit 的
        # branch / cleaned 在 git 通道下永远是 None / False，
        # 即便分支真的被建出来并带上了内容。
        global _LAST_TARGET_REF
        _LAST_TARGET_REF = target_ref

        # 本地也落一个提交，让工作区重回干净（下次 detect_changes 才准）。
        # 注意：本地历史与远端历史并无父子关系，纯粹当快照基线用。
        # 只提交 todo 这些路径，避免把别人先前 git add 进暂存区的文件一起卷进来。
        #
        # 这两步也要在 try 内：PATCH 成功后分支上就有内容了，此时中断
        # （Ctrl-C 最容易发生在这里 —— 大文件刚传完）会留下一个**有内容
        # 但没人知道**的孤儿分支。报告第 8 轮实测确认过这个残留点。
        # 三条硬约束（实测踩过：--hold 推 75 个文件、其中 1 个被
        # .gitignore 屏蔽 → git commit 因单个 pathspec 不匹配整体失败，
        # 75 个一个都没落，而脚本照常打印 ✅）：
        #
        # ① 被 .gitignore 屏蔽的路径**不递给** git add / commit。
        #    它们是「点名即意图」推上去的（等价 git add -f），远端层面
        #    保留；但本地 git 操作必须排除，否则一个坏 pathspec 拖垮整批。
        #
        # ② add 与 commit **分别**判定。实测正是 add 部分成功、
        #    commit 整体失败 —— 只判 add 会漏掉真正的失败点。
        #
        # ③ 成败由 **HEAD 是否真的前移** 判定，不看退出码：
        #    退出码 0 也可能是 no-op，非 0 也可能部分成功。
        #
        # 不加 --no-verify 是**有意的**：有 pre-commit hook 的仓库里 hook
        # 失败会让 commit 不落，但那是 hook 在替用户把关，绕开它等于
        # 把「lint/格式化不过」的改动硬塞进历史。加了 ②③ 之后 hook 失败
        # 会被如实报出来（不再是静默 ✅），用户可以选择手工处理。
        git_todo = [p for p in todo if p not in ignored_files]

        # N55：删除的路径必须**同步到本地 git 索引**，否则幽灵 "D" 永久残留。
        #
        # 实测：`rm a.txt` 后脚本只 commit 了 todo 里的路径（pathspec 提交），
        # a.txt 从未进入本地提交 → `git status` 永远显示 " D a.txt"。
        # 而脚本全文没有 git rm / git add -A / git add -u，于是删除路径
        # **永远不会**进本地提交。
        #
        # 两个后果：
        #  ① 谎报：删除功能关闭时打印「a.txt 仍留在远端」，而它早已在远端
        #     删掉、基线也 pop 了 —— 这句话是假的，且每次无改动推送都重复。
        #  ② 空提交：删除功能开启时 _pending_dels 非空 → 不早退 → 建空 tree
        #     → 空 commit → 空 PR。设了 PUSH_API_ENABLE_DELETE=1（文档称
        #     「常开场景」）后每次无改动推送都会来一轮。
        #
        # 修法：对本次**确实删掉**的路径执行 git rm --cached（工作区已无
        # 此文件，从索引移除即等价于记录删除），再让它们参与 pathspec 提交。
        _rm_done = [p for p in (_rm_paths or []) if p]
        if _rm_done:
            _rm_idx = _rm_done
            # N133：_git_run **从不抛异常**（返回 (rc, out, err)），
            # 于是外裹的 try/except 是永不触发的死代码 ——
            # git rm --cached 失败时完全静默。
            # 后果：删除已落远端，本地索引仍留着条目，
            # 下次 detect_changes 又把它们列出来，用户以为没删掉。
            _rc, _o, _e = _git_run("rm", "-q", "--cached", "--", *_rm_idx)
            if _rc != 0:
                # M95（A99，P3）：`git rm --cached` 对**已经从索引移除**
                # 的路径必然失败（rc=128「pathspec ... did not match any
                # files」）—— 而那恰恰是**我们想要的状态**，不是失败。
                #
                # 实测（真实 git 仓库，5 个场景）：
                #   已 staged（git rm / git add -A）+ PR --hold  → 失败 ★
                #   已 staged + PR 立即合并                      → 失败 ★
                #   已 staged + direct                           → 失败 ★
                #   未 staged + PR --hold                        → 成功
                #   未 staged + direct                           → 成功
                # 即：**只要用户按 git 的常规做法先把删除 stage 了**，
                # 就必然触发这条报警。而它三句全是错的：
                #   · "远端已删除" —— PR 工作流下要等合并，此刻还没删
                #     （同一次输出里上面 8 行刚说过"将随 PR 合并生效"）
                #   · "本地索引仍保留条目" —— 正相反，索引里已经没有了，
                #     这才是 rm --cached 报 pathspec 不匹配的原因
                #   · "可手工执行 git rm --cached -- a.txt" —— 这条命令
                #     会以**同样的错误**失败
                #
                # 判据改用「索引里到底还有没有它」，而不是 rm 的退出码：
                # 没有 = 已是目标状态，不报警；还有 = 真失败，且此时
                # "索引仍保留条目"才成立。
                _still = []
                for _p in _rm_idx:
                    _rc2, _o2, _e2 = _git_run("ls-files", "--error-unmatch",
                                              "--", _p)
                    if _rc2 == 0:
                        _still.append(_p)
                if _still:
                    print(f"  ! git rm --cached 失败（退出码 {_rc}）："
                          f"{(_e or _o or '').strip()[:160]}")
                    if wf == "pr":
                        print("    这些路径**尚未**从远端删除（PR 合并后"
                              "才生效），而本地索引仍保留条目 —— 合并前"
                              "跑 --pull 会把它们当成「远端新增」拉回本地。")
                    else:
                        print("    远端已删除，但本地索引仍保留条目；"
                              "下次可能仍被列为待推送。")
                    print("    可手工执行："
                          f"git rm --cached -- {' '.join(_still[:5])}")
            # 也要参与 pathspec，否则 commit 只提交 git_todo 那部分
            git_todo = git_todo + [p for p in _rm_idx if p not in git_todo]

        head_before = _local_head()
        # global 声明放在块首：下面两处都要赋值，写在赋值点前面会撞
        # SyntaxWarning（同一作用域内 global 声明晚于首次使用）。
        if not git_todo:
            local_commit_state = "skipped"
        else:
            added = _git_add_paths(git_todo)
            # 判据必须是「暂存区有没有东西」，**不能**看 add 的退出码：
            #
            # 路径匹配 .gitignore、但文件**已被 git 跟踪**时，`git add`
            # 照样把它加进暂存区，却返回退出码 1（"paths are ignored"）。
            # 只看退出码会把「add 其实成功了」误判成 add_failed，
            # 于是不再 commit —— 改动留在暂存区、脚本却说失败。
            # （实测：state/keep.json 已跟踪、state/ 被忽略 → add rc=1
            #   但 `git status` 显示已暂存。）
            #
            # 反过来，未跟踪且被忽略的路径已被 gitignored_set 排除，
            # 不会出现在 git_todo 里 —— 那才是真正会让 commit 因
            # pathspec 不匹配而整批失败的那一类。
            # add 一返回就清缓存，不等 commit：
            # ① add 已经改了 index，旧的 detect_changes 快照本就过期；
            # ② 清在 commit **之后**的话，「缓存是否失效」就依赖
            #    if/elif 分支的执行顺序 —— 而后面还要调 _local_head()
            #    判断 HEAD 有没有前移，那时缓存仍是旧的。
            #    把正确性押在语句顺序上是脆弱的隐式依赖（_write_local
            #    那处的注释讲的就是同一件事）。
            _RT.invalidate_changes()
            _, staged, _ = _staged_any(git_todo)
            if not staged.strip():
                # 暂存区没东西：add 正常就是「内容与 HEAD 一致」，
                # add 失败才是真问题。两者都要说清，不能都当成功。
                local_commit_state = "noop" if added else "add_failed"
            else:
                cname, cmail = _commit_identity()
                # N83：改走 _git_commit_paths —— ≥200 路径时与 add 同口径
                # 走 --pathspec-from-file，不再把上万条路径平铺进 argv。
                rc, _, cerr = _git_commit_paths(msg, git_todo, cname, cmail)
                if rc != 0:
                    # commit 失败也要把 stderr 说出来：git 的
                    # 「pathspec ... did not match」看不出真正原因
                    # （真正原因是该文件被 .gitignore 屏蔽）。
                    print(f"  ! 本地 git commit 失败（退出码 {rc}）"
                          f"：{(cerr or '').strip()[:300]}")
                    local_commit_state = "commit_failed"
                elif _local_head() == head_before:
                    # 退出码 0 但 HEAD 没动 = no-op，不能算成功
                    local_commit_state = "noop"
                else:
                    local_commit_state = "ok"
            # 缓存已在 add 之后清过（见上方注释），此处不重复。

    # 清理范围**停在「开 PR」之前**是有意的：PR 一旦建出来就有编号、
    # 能被 --prune 看见、也能 --merge 继续 —— 那时该做的是提示用户，
    # 而不是悄悄删掉。
    except _Cancel:
        # 正常取消：与失败走同一条清理通道，但**不**冒泡成错误。
        # 顺序必须在 except BaseException 之前，否则 _Cancel 会被它先接走。
        #
        # N362（第一百一十三轮，中危）：取消后退出码是 **0**。
        # 而 0 = 成功 —— CI 判定推送成功，远端一个字节没变。
        # 脚本还贴心地提示"加 --yes"，但 CI 不读提示，只看退出码。
        #
        # 与 N240 同族（N240 是"被拦下"返回 0，M42 已修）；本条是
        # "被取消"返回 0，是同一危害的另一条路。
        # 置位供顶层转退出码 4（见 __main__）。
        global _LAST_RUN_CANCELLED
        # M104：--dry-run 走的也是这条通道，但它不是"取消"。
        # 置位会让 --dry-run 的退出码变成 4（实测：加不加 --yes 都是 4），
        # 而输出里写着「[dry-run] 未做任何推送」、零「已取消」字样 ——
        # 见到的与退出码说的正好相反。
        if not _DRY_RUN_ONLY and not _NOTHING_TO_PUSH:
            _LAST_RUN_CANCELLED = True
            # M162（A148，中危）：「脚本主动拦下」走的也是这条 _Cancel 通道
            # —— `_cancel_blocked()` 先置位 `_BLOCKED_BY_GUARD` 再抛，于是
            # revert / unrecorded / direct-blocked 三处落到**这一行**。
            #
            # 而这一行原本无条件写 `push_cancel`，与用户按 N 取消**逐字相同**：
            # 事后想回答「那次是我没推，还是脚本不让我推」，审计答不了。
            #
            # 值得立条而不是只算措辞：① 审计是这条信息的**唯一载体**
            #（屏幕上的 ❌ 随终端关闭即消失，docstring 把审计定位为"事后
            # 要能回答那次推到哪一步了"的凭据）；② 与退出码叠加时双重误导
            # ——N566 已把退出码修成 2，但审计仍说"取消"，两个渠道给出
            # 的结论仍然不一致：一个说"被拦下"，一个说"用户没推"。
            #
            # 分两种 kind（而不是只加 reason 字段）：`_KIND_LABEL` / 查看器
            # 按 kind 分支，混在同一种里就要改所有消费方；而两者的处置
            # 完全不同（取消→重跑；拦下→--pull）。
            if _BLOCKED_BY_GUARD:
                audit({"kind": "push_blocked",
                       "branch": branch_created_now,
                       "guard": _BLOCKED_BY_GUARD})
            else:
                audit({"kind": "push_cancel", "branch": branch_created_now})
        _cleanup_failed_branch(branch_created_now, head_sha, pushed_commit)
        return
    except BaseException as _e:
        # 失败必须留下凭据：远端可能已经建了分支/blob，而用户看到的
        # 只是一堆报错。事后要能回答「那次到底推到哪一步了」。
        #
        # N383：这里**只覆盖 try 之内**的失败（建分支 → 建对象 → PATCH
        # → 本地补提交）。收尾那三步在 try 之外，由 _main_wrapped 的
        # 兜底 except 管 —— 两处都记会重复，所以用 _PUSH_FAIL_AUDITED
        # 去重（判据：一次运行一条 push_fail）。
        global _PUSH_FAIL_AUDITED
        _PUSH_FAIL_AUDITED = True
        audit({"kind": "push_fail",
               # N384：用 _LAST_TARGET_REF or branch_created_now ——
               # git 通道下 branch_created_now 恒 None，而此时分支确实
               # 已经被 push 建出来并带上了内容。fallback 保留是为了
               # 「还没推到远端就失败」那一侧仍记预建的分支名。
               "branch": _LAST_TARGET_REF or branch_created_now,
               "err": f"{type(_e).__name__}: {_e}"[:200],
               "cleaned": (_LAST_TARGET_REF or branch_created_now) is not None})
        _cleanup_failed_branch(branch_created_now, head_sha, pushed_commit)
        raise

    # -------- PR 工作流：开 PR → 自动合并 → 删分支 --------
    if wf == "pr":
        # 推到分支时主干尚未改变：base_commit / synced_commit 都要记**主干**，
        # 不能记分支上的 commit（旧版记 commit["sha"]，于是 P0-1 拿它与主干
        # head_sha 比较，必然不等 → 把刚推完的用户又挡在门外）。
        # 本地确实是「主干 head_sha 的内容 + 已推到分支的改动」，记主干才准确。
        #
        # 但**基线 files 不能**记成本次推送的内容：它的语义是「远端该文件的版本」，
        # 而这里远端还是旧主干。记成推送内容后，--pull 会看到
        # local == base 而判定「本地无改动」，直接用主干（他人的）版本覆盖本地，
        # 把你刚推到分支的改动静默抹掉 —— regression 实测过。
        # 正确时机是 --merge 成功之后，见 do_merge 里的 _refresh_main_baseline。
        state["base_commit"] = head_sha
        state["synced_commit"] = head_sha
        state["local_head"] = _local_head()

        # -------- P1-9：分支已 PATCH 上内容，先把状态落盘 --------
        # ensure_pr 在这之后才跑。若它失败而此处尚未落盘，state 里既没有
        # base_commit/synced_commit、也没有 tasks[branch]，于是：
        #   · 下次推送会被 P0-1 重新拦下一个「其实已经推到分支上」的改动
        #   · 用户完全不知道分支上已经有内容
        # 所以先把「已同步」和任务登记写盘，再去做开 PR 这件可能失败的事。
        state["workflow"] = "pr"
        # local_seen：见 mark_synced 处的说明，与基线同点更新。
        #
        # M192（A165，中危）：这里原本用 `lmap` —— 而 N356 在 direct 那一处
        # 的注释里明确判定「PR 分支的 lmap 写法就是 N242 的坑」，按它自己
        # 的判据**这一处应当一并改**，实测却一行没动。PR 是**默认工作流**，
        # 影响面比 direct 更大。
        #
        # 真正会漏的不是 N356 注释说的「本次刚推上去的文件」（那类旧快照
        # 里有：`local_state_map()` 过 detect_changes(refresh=True)，
        # `--untracked-files=all` 会把未跟踪新文件算进 sha），而是
        # **靠 `git add -f` 强推的被 .gitignore 屏蔽的文件** ——
        # git status 不列被忽略的文件，`git add -f` 之后它才进索引。
        # 实测：旧快照 keys 无 ig.txt，刷新映射有。
        #
        # 后果链：PR + 强推被屏蔽文件 → 该文件进不了 local_seen →
        # 日后删它时判据 3（「本地曾经拥有过」）失败 → 一个都删不掉，
        # 而屏幕还把原因归因反了方向。
        #
        # 注意：不是所有 `lmap` 都陈旧 —— 7 处调用点里 4 处用 lmap，
        # 其中 1 处是两行前刚取的新值。**判据是「是否陈旧」而非变量名。**
        _sync_local_seen(state, local_state_map()) if _sync_local_seen else None
        (state.setdefault("tasks", {}).setdefault(target_ref, {})
         ).update({"last_push": _now(),
                   # 记下分支 head：abandon_task 删分支前据此判断
                   # 「分支上是否有别人后来推的改动」（B5）。
                   "head": ref_sha(target_ref),
                   # 记下本次推到分支的内容：--merge 成功后据此判断
                   # 「主干上这个版本就是我推的」，从而安全地刷新基线。
                   # N65：删除条目也在 entries 里（追加在 todo 之后），
                   # 而 enumerate(todo) 只索引前 len(todo) 个 →
                   # 删除路径**永远**进不了 pushed。
                   # 后果：_merge_preflight 的 mine 集合漏掉删除路径，
                   # 主干若动了它们会被判 clean（应为 overlap），
                   # 于是"拦下保留"降级成"自动合并"。
                   #
                   # N67：`dict.update` 是**替换**而非合并。--hold 复用分支
                   # 时第二次推送会把第一批的 pushed 整体覆盖，
                   # 而分支上同时装着两批改动 —— 早期推送的文件不再参与校验。
                   # 改为与旧值合并。
                   "pushed": dict(
                       (state.get("tasks", {}).get(target_ref, {})
                        .get("pushed") or {}),
                       # N200/N207：pushed 完全依赖 entries，entries 一空
                       # 就成功推送却零记账 —— --hold 复用分支时后续校验
                       # 失去依据。因此补 `_pushed_shas()`：entries 为空时
                       # 退回本地索引 sha（内容寻址，与远端一致）。
                       #
                       # N267：上面"entries 空"这句在 **N193 时代**才成立。
                       # N207 之后 blob 循环无条件执行、entries 始终完整，
                       # 所以现在**两个通道都走 entries 那一支**。
                       # fallback 只作防御保留（并已补上删除条目）。
                       **(_pushed_shas(entries, todo, lmap)))})
        save_state(state)

        # -------- P1-8：开 PR 失败 → 有内容、无 PR 的分支 --------
        # 分支已建且已 PATCH 上 commit。若开 PR 这步失败（网络抖动 / 422），
        # 远端留下的是**装着本次全部改动**的分支，而 --prune 会把它判成
        # 「孤儿分支」并建议 --delete-branch —— 工具引导用户删掉自己的东西。
        # 所以这里不能静默，也不能沿用「失败即清理」（那会删掉有内容的分支）。
        try:
            pr, _ = ensure_pr(target_ref, msg)
        except BaseException:
            print(f"\n⚠ 分支 {target_ref} 上已成功推入本次改动，"
                  f"但开 PR 失败。")
            print("  改动没丢，仍在分支上。请这样继续：")
            print(f"     python3 push_api.py --branch {target_ref} -m \"{msg}\"")
            print(f"  ⚠ 不要 --delete-branch {target_ref}（那会删掉本次改动）。")
            raise
        state["pr_number"] = pr["number"]
        (state.setdefault("tasks", {}).setdefault(target_ref, {})
         ).update({"pr": pr["number"]})
        save_state(state)
        # N142：删除是脚本里**唯一不可逆**的远端操作，却在成功的审计
        # 记录里完全缺席（只记了新增数量 len(todo)）。
        # 事后要回答"远端那个文件是几号被谁删的"，流水里查不到。
        audit({"kind": "push_ok", "branch": target_ref,
               "commit": commit["sha"], "files": len(todo),
               "deleted": list(_rm_paths or []),
               "workflow": wf})
        # 结果窗口（唯一出口）：推到哪 / PR / 本地补提交 / 待删除清单
        # / 后续动作，一次说清。此前这些内容散在好几处各打各的。
        # N524（第一百七十三轮，中危）：A6-J2 把「登记」与「生效」拆成
        # 两处，而**两处落在不同分支上，永远合不上**。
        #
        # 实测（真实 _main，`_apply_orphaned_task` 旁路记录）：
        #   A：--hold + --new-task（--new-task 唯一有效的场景）
        #      ★ _apply_orphaned_task 调用次数: 0
        #      ★ 输出含『已不再接管』: False
        #      ★ 旧分支仍在 tasks: True
        #      ★ 输出含『放弃当前任务分支』: True   ← 声明照打
        #   C：direct 路径（对照）→ 调用 1 次，但登记值是 None（登记点
        #      在 pr 块内，direct 不经过）→ 函数第一行就 return。
        #
        # 登记点在 `if wf == "pr":` 里（14938），生效点在两个 pr 块**之外**
        # （16741，direct-only）；而 PR 路径在本行之前的 return 就走了。
        # 于是：旧分支永久留在 state["tasks"]，「已不再接管」这句声明
        # 在真实流程里永远打不出来，而 13588 那句「放弃当前任务分支」照打
        # —— 用户被告知"放弃了"，磁盘上却仍接管着。
        #
        # （而 --prune 也报不出来：它的判据是「远端不存在才从 tasks 摘除」，
        #  旧分支远端仍在、PR 未合并。两条告知通道同时失效。）
        #
        # 修法：PR 路径在**推送确实完成**之后（本行）、两个 return 之前
        # 生效一次。函数内部会清 _ORPHANED_TASK_OLD，重复调用无害。
        #
        # N637（A134 的 M143 复核，中危）：**不能**在这里无条件调用 ——
        # 本行在两个 return **之前**，也就是"推送即合并"那条路上的
        # merge **还没跑**。merge 失败时（冲突 / 限流 / 网络）分支与 PR
        # 都还在，那时摘掉旧分支会把用户唯一的追踪入口抹掉：
        #   · `_apply_orphaned_task` 打印「可用 --prune 查看」，而 --prune
        #     的判据是「远端不存在才从 tasks 摘除」—— 分支仍在 → 它不会
        #     列出这个分支，于是"还能查到"这句话对这一支是假的；
        #   · A134 的 F 组对照（merge 失败 + --new-task）明确要求调用 0 次，
        #     理由就是"宣布不再接管会把用户唯一的追踪入口抹掉"。
        #
        # 所以拆成两处，各自只在**事实已成立**之后调用：
        #   ① `--hold` 的 return 之前 —— 分支与 PR 已建好，hold 有意保留；
        #   ② merge 成功之后 —— 分支已删除，追踪入口确已转移到主干。
        # merge 失败那一支（下面的 else）**不调用**。
        _report_push_result(
            target_ref, commit["sha"], pr=pr, hold=bool(opts["hold"]),
            local_commit_state=local_commit_state, todo=todo,
            ignored_files=ignored_files, msg=msg,
            pure_delete=bool(_rm_paths) and not todo,
            state=state, head_sha=commit["sha"])
        if opts["hold"]:
            # ① 分支与 PR 已建好（本行之前已 ensure_pr + save_state）。
            _apply_orphaned_task(state)
            # N572（第一百八十七轮，中危）：`--hold` 是脚本明确支持的用法
            # （「攒够改动后…」），而它恰恰是**不合并**的那条路 ——
            # `_refresh_main_baseline` 只在 `do_merge` 成功时被调用，
            # 于是 C2 组实测：基线不刷新，且「N 个文件本地落后于远端」
            # 这句告知**完全消失**，整段输出只剩「分支与 PR 已保留」。
            #
            # 而 `--hold` 的设计目的就是「保留分支以便**后续补推**」——
            # 用户下一步就是补推，他却拿不到「e.txt 已落后于远端、
            # 推它会覆盖」这条信息。A 组（direct）给了这条并指明了
            # `--pull`，C2 组（PR + hold，即默认工作流）什么都没给。
            #
            # 用 `_classify_remote_drift`（只读）：这里**不**刷新基线 ——
            # 主干一个字节没变，刷了等于声明「本地已含远端最新」，
            # 而分支还没合进去，那是假同步声明（N460 那族）。
            _dr_h, _dn_h, _lag_hold = _classify_remote_drift(
                state, lmap, rstate, todo)
            if _lag_hold:
                print(f"\n⚠️  {len(_lag_hold)} 个文件本地落后于远端 "
                      f"{BRANCH}（--hold 未合并，基线**未**刷新，"
                      f"补推这些文件会覆盖远端）：")
                for _p in _lag_hold[:20]:
                    print(f"   - {_p}")
                if len(_lag_hold) > 20:
                    print(f"   … 其余 {len(_lag_hold) - 20} 个")
                print("   补推或合并前请先：python3 push_api.py --pull")
            # 摘掉 tasks 之后必须再落盘一次：上面 save_state 在本行之前。
            save_state(state)
            return

        # 推送即合并：冲突时 GitHub 返回 409，改动不会丢（还在本地工作区），
        # 所以可以安全地把分支和 PR 一起收掉，不留僵尸。
        if do_merge(state, pr["number"], opts["method"], yes=True, quiet=True,
                    keep_on_conflict=opts["keep_on_conflict"],
                    allow_stale=opts["allow_stale"]):
            print(f"✅ 已合并进 {BRANCH} 并删除分支 {target_ref}")
            # ② merge 成功 —— 分支已删除，事实成立。
            _apply_orphaned_task(state)
            save_state(state)
        else:
            # N637（接上）：merge 失败 → **不摘除**。分支与 PR 都还在，
            # 旧分支仍是用户追踪本次工作的唯一入口；摘掉它等于在
            # 「工作还没落地」的时候把索引先烧了，而上面那句
            # 「已不再接管（可用 --prune 查看）」在这条支路上是假的。
            # N376（第一百二十轮）+ N429（第一百四十轮）：合并没做成 →
            # do_merge 返回假 → 这里**什么都不打印** → return →
            # main() 正常返回 → 顶层两个判据都不成立 → **exit 0**。
            #
            # 而"推送即合并"这条路径上（`--hold` 已在上面 return 掉），
            # 主干一个字节没变，CI 拿到 0 会当主干已更新 —— 典型序列
            # 「推送 → 处理 → 验证」的第一二步之间的信号就断了。
            #
            # 这是 M75 担心的两种坏结局（噪音告警 / 为压噪音漏报）之外的
            # **第三种**：既不是 2 也不是 1，是 0 —— 比那两种更难发现。
            #
            # 置位后由 main() 的 finally 走「先取值 → 复位 → 写回」三步
            # 转移到 _LAST_RUN_BLOCKED（M42 的教训：直接在这里写
            # _LAST_RUN_BLOCKED 会被复位清掉）。
            #
            # 改动没丢（分支与 PR 都还在），所以是"拦下"语义 → 2，
            # 不是"出错" → 1。
            # （_main 顶部已声明 global _BLOCKED_BY_GUARD，此处不再重复。）
            if not _BLOCKED_BY_GUARD:
                _BLOCKED_BY_GUARD = "merge_not_done"
        return

    # -------- 更新基线（仅 direct 模式；PR 模式在 merge 成功后刷新）--------
    # N221：按**索引**配 entries[i] 依赖「entries 与 todo 一一对应」，
    # 而 N193（git 通道跳过 blob 上传）**曾经**打破这个前提 ——
    # entries 会比 todo 短，轻则取错文件的 sha，重则 IndexError
    # 落在推送成功之后（基线写不进去，下次全部重推）。
    #
    # N267：N193 已由 **N207 撤销**（见 `pushed` 赋值处与
    # `_pushed_shas` 的注释），现在 blob 循环无条件执行、entries 始终
    # 完整。下面改成按 path 建映射仍然是对的 —— 它不再被用来补 N193 的
    # 缺口，而是**不再依赖顺序与长度**，比索引配对更稳。
    #
    # 按 path 建映射取，不依赖顺序与长度。
    _by_path = {e.get("path"): e for e in entries if e.get("path")}
    _missing = []
    for rel in todo:
        e = _by_path.get(rel)
        if e and e.get("sha"):
            state["files"][rel] = {"mode": e.get("mode", "100644"),
                                   "sha": e["sha"]}
        else:
            _missing.append(rel)
    if _missing:
        # 不静默：基线缺这些条目会导致下次把它们当「远端独有」——
        # 那是 fail-closed（拦下），比写错 sha 安全，但要让人知道。
        print(f"  ! 基线未能记录 {len(_missing)} 个文件的 sha"
              f"（下次会按「远端独有」拦下，需 --pull）："
              f"{', '.join(_missing[:5])}")

    # 非本次推送、但远端已经变了的文件：只在「本地也已经是远端版本」时刷新基线。
    #
    # 无条件刷新的话（旧版行为）会把「本地落后于远端」这个事实抹掉：
    # 下次再推动该文件，第一层看到 远端 == 基线 就放行，本地旧内容 + 你的改动
    # 会整文件覆盖上去；同时 --pull 也认为「远端没变」不再处理，彻底锁死。
    # 不刷新则保持 fail-closed：第一层拦住 → 提示 --pull → 合并后自然解锁。
    # N572（第一百八十七轮）：分类逻辑抽到 `_classify_remote_drift`，
    # 与 PR `--hold` 出口、`--dry-run` 预览共用**同一份**判据。
    # 原来它写死在本分支里，于是那两条路各自拿不到结果 →
    # 「N 个文件本地落后于远端」这句在默认工作流（PR）下永不出现。
    #
    # 三类语义（详见该函数 docstring）：
    #   refreshed —— 本地已是远端版本 → 刷基线、进告知
    #   main_new  —— 主干新增（M165）→ 刷基线、**不**进 refreshed 告知，
    #                也**不**进 lagging（本地没有这个文件，不存在覆盖）
    #   lagging   —— 本地落后 → **不**刷基线（fail-closed）
    _drift_r, _drift_n, lagging = _classify_remote_drift(
        state, lmap, rstate, todo)
    for p, (m, s) in _drift_r.items():
        state["files"][p] = {"mode": m, "sha": s}
    for p, (m, s) in _drift_n.items():
        state["files"][p] = {"mode": m, "sha": s}
    refreshed = list(_drift_r)

    state["base_commit"] = commit["sha"]
    state["synced_commit"] = commit["sha"]     # 推送后本地即远端最新
    state["local_head"] = _local_head()
    # N355（第一百一十一轮，中危）：direct 分支**缺这一行**。
    #
    # `_main` 全函数只有 PR 分支里调了 _sync_local_seen，direct 一个都没有
    # —— 而 direct 分支确实在这下面写基线（state["files"][p] = …），
    # 是货真价实的基线刷新点，正是 sync_local_seen docstring 要求的
    # 调用位置（"必须与基线同点更新"）。
    #
    # 决定性对照（同一场景，只差工作流）：
    #   PR     : local_seen = ['a.txt', 'new.txt']   ✅
    #   direct : local_seen = ['a.txt']              ❌ new.txt 缺席
    #
    # 后果链（端到端实测）：direct 推了 new.txt → 它不在 local_seen
    # → 之后删掉它时，判据 3（local_seen 有过）失败 → **一个都删不掉**，
    # 而屏幕还说「（删除功能关闭时表示未被处理）」，把原因归因反了方向。
    #
    # N356（第一百一十一轮，低危）：补这一行时若直接照抄 PR 分支的
    # `lmap`，就又把 N242 的坑原样搬过来了 —— `lmap` 是 `_main` 早期取的
    # 快照，而 direct 走到这里时脚本已经 git add / git commit 过，
    # 工作区与索引都变了。用旧快照同步，"本次刚推上去的文件"可能照样
    # 进不了 local_seen，于是 N355 白修一半。
    # 与 N242 同一个口径：改了工作区就要重新看。
    _sync_local_seen(state, local_state_map()) if _sync_local_seen else None
    # A6-J2：登记点在建分支处（那时还不知道这次推不推得成），
    # **生效点在这里** —— 本次推送确实完成、状态文件即将写回之前。
    # 只有走到这里，「旧分支不再接管」才是一个已经成立的事实；
    # 取消 / 无事可做 / --dry-run 都到不了这一行，于是不再产生
    # 「声明已经弃用、磁盘上却仍接管」的矛盾状态。
    _apply_orphaned_task(state)
    save_state(state)

    # N476（A127 复核 156 轮）：**这里不需要 dry 分支** —— 不是漏了，
    # 是 dry 下根本到不了。
    #
    # A127 §2 建议补 `elif refreshed and dry:`（对照早退路径 `_n459` 那处
    # M119② 是成对改的）。A115 实测判定那是**死代码**，本轮（A127 复核）
    # 用探针副本做了决定性对照，A115 成立：
    #
    #   真实远端探针仓库 + `refreshed` 有值（d.txt 基线 sha 改旧）
    #     --direct --dry-run  → 探针一次都没打印（本段未执行）
    #     --direct（真推）    → 探针打印 `refreshed=1 dry=False`  ← 探针有效
    #
    # 原因：`--dry-run` 在更早处（下方「[dry-run] 未做任何推送」之后）
    # 就 `raise _Cancel()` 了，本段在其之后。**信息缺失、但不误导**
    #（dry 下屏幕明写「未做任何推送」）。
    #
    # 所以按 M8 / M37 / M76 的先例（「注释说它没用、代码还在，人会信代码」）
    # —— 不加那段分支，改用 `测试/test_a115_…` 里的结构性判据守着：
    # `refreshed` 的赋值点必须仍在 dry 早退点**之后**。将来若有人真去把
    # 计算提到 dry 早退之前，那条判据会红，提醒同步补 dry 文案。
    #
    # 附带实测事实（A115 / A127 都没记）：本段只在 **direct 模式**执行 ——
    # PR 模式在 `do_merge()` 成功后就 `return` 了（基线由 merge 路径刷新）。
    if refreshed and not dry:
        # M119②：同上 —— dry-run 下 save_state 是 no-op，这句是假话。
        print(f"\n  · {len(refreshed)} 个非本次推送的文件远端已更新，本地已是同一版本，基线已跟进")
    if lagging:
        print(f"\n⚠️  {len(lagging)} 个文件本地落后于远端（基线**未**刷新，推它们会覆盖远端）：")
        for p in lagging[:20]:
            print(f"   - {p}")
        if len(lagging) > 20:
            print(f"   … 其余 {len(lagging) - 20} 个")
        print("   改这些文件前请先：python3 push_api.py --pull")

    # N141：direct 推送**成功**时零审计记录 —— 而失败(push_fail)和
    # 取消(push_cancel)都有。更关键的是 direct 恰恰是更危险的那条路：
    # 脚本自己的注释写着「直推主干会绕过 PR 流程与全部覆盖防护，
    # 且失败后无法回滚」。PR 路径还有分支/PR 页面/合并记录可回溯，
    # direct 直推主干时，审计流水是**唯一的本地痕迹**。
    audit({"kind": "push_ok", "branch": target_ref,
           "commit": commit["sha"], "files": len(todo),
           "deleted": list(_rm_paths or []),
           "workflow": wf, "direct": True})
    # 与 PR 路径共用同一个结果窗口，避免两条路各写一份收尾。
    _report_push_result(
        target_ref, commit["sha"], direct=True,
        url=f"https://github.com/{OWNER}/{REPO}/commit/{commit['sha']}",
        local_commit_state=local_commit_state, todo=todo,
        ignored_files=ignored_files, msg=msg,
        pure_delete=bool(_rm_paths) and not todo,
        state=state, head_sha=commit["sha"])




# ================================================================
# ---------------------------------------------------------------- 删除与重命名
# ================================================================
#
# 本区块原先是独立文件 delete_rename_support.py，现已**合并进单文件**。
#
# 为什么合并（而不是继续做插件）：
#   1. 单文件部署是本工具的根本价值 —— 拷进任何仓库就能跑、零依赖、零安装。
#      拆成两个文件意味着要么带目录走、要么打包发布，等于毁掉它最实用的特性。
#   2. 原先靠 `importlib.import_module` 从 sys.path 加载，而 sys.path 包含
#      脚本所在目录 → 仓库里放一个同名文件就会被执行（任意代码执行面）。
#      合并后这个加载面**直接消失**，不再需要靠开关去防。
#   3. N47 自述的 `_encodable` / `_path_json_safe` 两份重复实现可以收敛成一份，
#      不必再靠注释约定「两边必须同步」。
#
# 安全模型的变化（重要）：
#   原来是「默认不**加载**」—— 代码根本不在内存里。
#   合并后代码总在，只能「默认不**启用**」—— 强度低一档。
#   因此删除逻辑必须有独立开关（PUSH_API_ENABLE_DELETE），且开关要包住
#   **所有副作用入口**，而不只是「跳过执行」：否则某个新增调用点忘了检查就漏了。
#
# ----------------------------------------------------------------------------
# N357（第一百一十二轮，中危）：下面这段原先是**裸字符串字面量**
# （三引号包裹），而 Python 只把模块里**第一个**字符串字面量当
# docstring —— 它排在第二，于是是一段**死字面量**，实测 __doc__ 里查不到：
#
#     __doc__ 长度: 5519
#       含「删除与重命名」: False
#       含「三信号交集」  : False
#       含「熔断」        : False
#
# 被埋起来的正是**最该看得见的那份**：删除是本脚本唯一不可逆的远端操作，
# 而「判据：三信号交集」「local_seen 为什么必须新增」「重命名怎么落地」
# 「熔断」这四节全在这里。
#
# 为什么不直接删：它是唯一系统讲清删除层取舍的材料，删了就没了。
# 为什么不塞进顶部 __doc__：那是 USAGE（用法），混进设计文档会毁掉它
# 作为速查表的价值。所以改成注释 —— 至少不再是"看着像 docstring 实则不
# 生效"的误导形态。
#
# 删除与重命名层的设计说明（原 delete_rename_support.py，现已合并进本文件）。
#
# N357-①：原先这里写的是「push_api.py **明确不支持**删除与重命名」——
# 那是合并**前** delete_rename_support.py 的开场白，合并时整段搬进来没改。
# 而删除（--delete / PUSH_API_ENABLE_DELETE）、重命名检测、LFS 拒绝推送
# 早已实现。这句把已实现且被反复审查的功能说成不存在，并据此给用户指路。
#
# ====================================================================
# 为什么需要这个模块
# ====================================================================
# N357-①（续）：下面这两行是**合并前**的现状描述，现已不成立 ——
# 删除与重命名都已实现，保留它们只为说明当初为什么要做这一层。
#     删除   → 本地删文件，远端永远留着（detect_changes 里 "D" 被 continue）
#     重命名 → 表现为「新增 + 残留」，远端长期漂移
#
# 删除是本脚本唯一**不可逆**的远端操作（虽然 commit 历史里还能捞回来，
# 但用户通常不会第一时间发现删错了），所以判据必须比普通推送严格得多。
#
# ====================================================================
# 判据：三信号交集，缺一不可
# ====================================================================
# 朴素做法（本模块早期版本）是从 `git status` 的 "D" 取删除信号。
# **这条路走不通**，实测：
#
#     删 a.txt，未 commit   → git status 显示 " D a.txt"     能发现
#     删 b.txt 并 commit    → git status 干净，一个字都没有   发现不了
#
# 而 push_api.py 推送成功后会自动补一个 commit，用户也可能自己先 commit。
# 两种情况下删除都会**静默丢失** —— 正是本脚本最想消灭的那类事故。
#
# 也不能只用「基线里有、本地 lmap 里没有」：那一类里还包含
# **远端有、本地从来没有过**的文件（init_baseline 明确把它们记进基线、
# 但不算 diff，避免全新目录首次 init 被永久拦住）。照这个判据删，
# 等于删掉用户从没见过的远端内容。
#
# 所以本模块用三信号交集：
#
#     1) 基线里有记录          —— 知道它原来是什么样子
#     2) 本地 lmap 里没有      —— 本地确实不存在了
#     3) local_seen 里有       —— 本地**曾经**有过（排除「远端独有」）
#
# 再叠加两道常规闸门：
#     4) 远端当前 sha == 基线 sha   —— 期间没被别人改过（force 可越过）
#     5) 不在 state["conflicts"] 里 —— 有未解决冲突时不碰删除
#
# 任一条不满足 → 不删，并报告原因。宁可残留一个文件，
# 也不能删掉别人刚加的内容。
#
# **判据 1/3 必须在 `_guard` 里统一执行**，不能只在 `collect_removal_candidates`
# 里查。原因：调用方通过 `deleted=` 传入的路径**原样并入**，不走那个函数，
# 于是「远端独有」的文件可以借这条参数路径绕过判据（实测复现）。
#
# ====================================================================
# local_seen：为什么必须新增这个状态
# ====================================================================
# 它是「本地曾经拥有过哪些路径」的累积集合，每次同步成功时用 lmap 更新。
#
#     · 纯附加字段：老状态文件没有它 → 按「从没见过」处理 → 一律不删。
#       方向是安全的（漏删可补，误删不可逆）。
#     · 只增不减，但会被裁剪到「仍在基线里**或**本地还在」的路径，
#       避免仓库删文件后这个集合无限膨胀。
#
#     **为什么是「或」而不是只留基线内**（实测踩过）：
#     只裁到基线内会把「刚推上去、还没进基线」的新文件当成垃圾丢掉 ——
#     PR 工作流要等 merge 成功才刷新基线，中间这段窗口里它们只在 lmap 里。
#     裁掉之后，日后这些文件被删时会判成「远端独有」而漏删。
#
#     · 写入时机与基线刷新严格同点（见 sync_local_seen 的注释）：
#       晚一步就会把「刚推上去、还没落进基线」的记录冲掉。
#
# ====================================================================
# 重命名怎么落地
# ====================================================================
# 重命名 = 「删除旧路径 + 新增新路径」，但要识别出来才好提示。
# 做法：对「本地消失的路径」与「本地新增的路径」按 blob sha 配对：
#     sha 相同  → 判为重命名（一次提交里同时删旧、加新）
#     sha 不同  → 判为「删除 + 新增」两个独立动作
#
# 纯重命名（内容未改）不产生数据风险；改名同时改内容则 sha 不同，
# 会退化成删+增，行为与现状一致，不会更差。
#
# **重命名也必须过校验**（M2/M4）：旧路径被他人改动时不能删；
# 新路径已被他人占用且内容不同时，删旧 + 推新等于覆盖对方文件。
# 实测早期版本只查旧路径，于是「远端 new.txt 已存在不同内容」时照样放行。
#
# ====================================================================
# 熔断
# ====================================================================
#       · 真删除数超过 DELETE_MAX_FILES        → 拒绝
#       · 真删除数占基线比例超过上限            → 拒绝（仅大仓库启用）
#       # N340（第一百〇六轮，低危）：上面这行原本写的是
#       #   「删除条目总数（含重命名）占基线比例超过 TOTAL_MAX_RATIO → 拒绝」
#       # 而 TOTAL_MAX_RATIO **全文件只在这一行出现** —— N122 早已把第三条
#       # 改成绝对数（`n_total > lim["max_files"] * _tf and n_pure > 0`）。
#       # 描述的是**已被替换的判据**，照它去调环境变量会一无所获。
#       · 删除条目**总数**（含重命名）超过 DELETE_MAX_FILES 的放宽倍数
#         （PUSH_API_DELETE_TOTAL_FACTOR）且确有真删除 → 拒绝
#
#     场景：git 读失败 / 切错 --root / 误用了别的基线文件，
#     会把「整个仓库都消失了」当成一次正常删除，一次推上去全删光。
#
# **第三条是补的**（H1）。原来只看「真删除数」，而重命名的旧路径不计入，
# 于是只要每个待删路径都能在 added 里找到同 sha 的新路径（`__init__.py`、
# `.gitkeep`、重复 LICENSE、空占位文件都容易同 sha），n_pure 就归零，
# **前两条同时失效** —— 实测 40 删 + 40 同 sha 新增，40 个条目全部放行。
#
#     完全豁免重命名也不对：整目录移动（git mv src/ lib/）是合法场景，
#     移动 30 个文件不该熔断。所以按**总数**卡（而不是纯真删除数）：
#        · 200 个文件的仓库移动 25 个 → 25 < 上限，正常
#        · 40 个文件的仓库「平移」40 个 → 40 > 上限，异常
#     （N122 起改用绝对数 + FACTOR 放宽倍数据，不再是占基线比例）
#
# 阈值可用环境变量覆盖（无需改源码）。N357-③：原先这里只列了 3 个，
# 而 limits() 里真生效的是 **6 个**，漏列的 3 个恰恰是后几条熔断的旋钮 ——
# 用户想放宽「比例熔断的最小仓库规模」时，文档里根本没有这个名字：
#     PUSH_API_DELETE_MAX_FILES          真删除绝对上限（第一条）
#     PUSH_API_DELETE_MAX_RATIO          真删除比例上限 (0,1]（第二条，N51：不接受 0）
#     PUSH_API_DELETE_RATIO_MIN_FILES    第二条生效的最小基线文件数
#     PUSH_API_DELETE_RATIO_MIN_PURE     第二条生效的最小真删除数（M7）
#       ⚠ M197（A167）：判据是 **OR**，调大它=让第二条**更难启用** ——
#       在 total_base < ratio_min_files 的小仓库里等于**关掉整条比例熔断**
#       （实测：base=15、删 12 个 = 80%，默认 abort；设 13 或 100 → 零熔断）。
#       它同时是「加严旋钮」和「关闭开关」，后者原先任何一处都没写。
#     PUSH_API_DELETE_TOTAL_FACTOR       第三条的放宽倍数（N325 起真实生效）
#     PUSH_API_DELETE_WIPE_MIN           「删空」判据的最小文件数（N39）
#
# 判据：这一份必须与 limits() 的键**逐一对齐**。
#
# M76（A80）：上面这句原先写「已由 check_invariants 判据『文档列出的旋钮
# 必须有读取点』覆盖（N357 起）」—— **只覆盖了一半**。那条判据查的是
# 「文档写了、代码零读取」（列了不存在的旋钮，照它调没反应）；
# 「代码有、文档没写」那一侧**无人守**（M76 已把那段恒空的死分支删掉）。
#
# 所以对齐这件事的**责任在这里**：往 limits() 加旋钮时，同一次改动必须
# 把它加进上面这份清单，否则用户不知道能调 —— 不会有判据替你报。
# （N357-③ 就是把漏的 3 个补回来的那次；补回来的依据是人看出来的。）
#
# ⚠ 这一行**曾经列过两个不存在的旋钮**（N325，第九十九轮，低危）：
#     PUSH_API_DELETE_TOTAL_RATIO   —— 代码**零读取**
#     PUSH_API_DELETE_TOTAL_FACTOR  —— 读进 lim["total_factor"]，而熔断处
#                                      用的是常量 DELETE_TOTAL_FACTOR，
#                                      该键**全文件 0 处引用**（已接线）
# 在"无需改源码"这一句下面列一个不存在的旋钮，用户照它去调熔断上限
# **什么都不会发生** —— 这正是 N272 报过的死常量（当时是残留 docstring
# 里写着这条判据），判据本身已改、文档没跟着走。
#
# 已删。与 N261（CLI 选项未进 --help）、N298（隐藏别名）同一族：
# **文档承诺的旋钮必须真的有读取点**，否则比不写更糟。


DELETE_MAX_FILES = 20        # 单次最多真删多少个（不含重命名），超过熔断
# 真删除数占基线文件总数的比例上限。
# 环境变量 PUSH_API_DELETE_MAX_RATIO 可覆盖。
# ⚠ 取值区间是 **(0, 1]**，不接受 0（N51）：_env_float 用 `0 < v <= 1`
# 判定，0 会被当成非法值回落到默认 0.10 —— 文档原写「0~1」会误导。
# 想关掉比例熔断请设一个很大的值（如 1.0），而不是 0。
DELETE_MAX_RATIO = 0.10
# 比例熔断的生效门槛：小仓库只用绝对数量兜底。
# 环境变量 PUSH_API_DELETE_RATIO_MIN_FILES 可覆盖（N25：此前未在
# docstring 声明，也没测试覆盖 —— 用户无法知道这个门槛可调，
# 而小仓库删 1 个就占 50%，正是最容易被误拦的场景）。
DELETE_RATIO_MIN_FILES = 20
DELETE_TOTAL_FACTOR = 3      # 删除条目总数上限 = 真删除上限 × 本系数
# 整仓删除（删空）判据的生效下限。低于这个规模的小仓库全删属正常操作
# （清理一个只有两三个文件的仓库），拦它是纯误伤；达到这个规模还全删，
# 才更像「git 读取失败 / 切错 --root」导致的批量误判。
# 环境变量 PUSH_API_DELETE_WIPE_MIN 可覆盖。
DELETE_WIPE_MIN_FILES = 5

# M7：比例判据的**分子**门槛。仓库不够大（< ratio_min_files）时，
# 只要真删除数量到了这个数，比例判据照样启用 ——
# 「删 17 个」无论仓库多大都是批量操作，不该因仓库小而跳过熔断。
# 环境变量 PUSH_API_DELETE_RATIO_MIN_PURE 可覆盖。
# 值在 _delete_limits() 里按运行时读取（此处 _env_int 尚未定义）。
DELETE_RATIO_MIN_PURE = 10

# mode → Git Data API 的 type
# 删除条目的 type 必须跟着 mode 走，否则同一条目里「一半尊重历史、
# 一半不尊重」（N4）。submodule 的 type 是 commit，不是 blob。
TYPE_BY_MODE = {"160000": "commit", "040000": "tree"}
SUBMODULE_MODE = "160000"


def _env_int(name, default):
    """环境变量覆盖阈值。解析失败一律用默认值 —— 阈值配错不该静默变成 0
    （0 会让任何删除都触发熔断）也不该变成无穷（等于关掉熔断）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def _env_float(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if 0 < v <= 1 else default


def limits():
    """当前生效的熔断阈值（已应用环境变量覆盖）。"""
    return {
        "max_files": _env_int("PUSH_API_DELETE_MAX_FILES", DELETE_MAX_FILES),
        "max_ratio": _env_float("PUSH_API_DELETE_MAX_RATIO", DELETE_MAX_RATIO),
        "ratio_min_files": _env_int("PUSH_API_DELETE_RATIO_MIN_FILES",
                                    DELETE_RATIO_MIN_FILES),
        "total_factor": _env_int("PUSH_API_DELETE_TOTAL_FACTOR",
                                 DELETE_TOTAL_FACTOR),
        "wipe_min_files": _env_int("PUSH_API_DELETE_WIPE_MIN",
                                   DELETE_WIPE_MIN_FILES),
        "ratio_min_pure": _env_int("PUSH_API_DELETE_RATIO_MIN_PURE",
                                   DELETE_RATIO_MIN_PURE),
    }


# ---------------------------------------------------------------- 状态辅助

def _entry_sha(v):
    """基线条目 → sha。兼容三种形态。

    - {"mode": ..., "sha": ...}（state["files"] 的现行写法）
    - 裸 sha 字符串（v1 形态）
    - (mode, sha) 元组（`local_state_map()` / `remote_state_map()` 的写法）

    第三种此前被漏掉：元组不是 dict，原实现**原样返回整个元组**。
    返回值于是永远不等于任何 sha 字符串 —— 判据静默偏向「不等」那一侧。
    实测（N592 收尾）：`--scope` 侧把元组形态的条目当 base 传进来后，
    **真搬家**被判成失效、「已判为搬到」标注消失（test_scope_moved_away
    N472④ / N473④ 两条红的直接原因）。
    """
    if isinstance(v, dict):
        return v.get("sha")
    if isinstance(v, (tuple, list)) and len(v) >= 2:
        return v[1]
    return v


def _entry_mode(v):
    return v.get("mode") if isinstance(v, dict) else None


# N47：本函数原先与 push_api._path_json_safe 是两份重复实现，靠注释约定
# 「两边必须同步」—— 而这类约定迟早会漂移（判据一旦不一致，就会出现
# 「push_api 认为安全、删除逻辑认为不安全」的错配）。
# 合并进单文件后直接收敛成一份：保留 _path_json_safe 为准。
_encodable = _path_json_safe



def sync_local_seen(state, lmap):
    """把当前本地存在的路径并入 state["local_seen"]。

    必须与「基线刷新」在**同一个点**调用：本函数回答的是
    「哪些路径本地曾经有过」，而基线回答「远端有什么」。两者若错位
    （比如先刷基线、后更新 local_seen），刚推上去的文件会在基线里
    却不在 local_seen 里 —— 下次它若被删，会被判成「远端独有」而漏删。

    返回被非 UTF-8 过滤掉的路径数（供调用方提示，N7）。
    这些文件永久无法被删除检测覆盖，用户有权知道。
    """
    if not isinstance(state, dict):
        return 0
    base = state.get("files") or {}
    seen = set(state.get("local_seen") or [])
    seen |= set(lmap or {})
    # 无条件裁剪（L1）：base 与 lmap 都空时 keep 也为空，此时若跳过裁剪，
    # local_seen 里的幽灵路径会被原样保留（实测 ['ghost1','ghost2'] 残留）。
    # 那种情况下没有任何参照能证明它们存在过，清空才是对的。
    keep = set(base) | set(lmap or {})
    # N10：这里的裁剪一度是「永久且不可恢复」的 ——
    # 实测链路（PR 工作流）：删除并推上分支 → 基线过早 pop（第三轮 N2）
    # → 本函数把 a.txt 裁掉 → PR 未 merge 时 --pull 把主干上的 a.txt
    # 写回本地并记进基线 → 用户再次删除 → plan_removals 因判据 3
    # 认为「本地从未拥有过」→ entries=[] skipped=[]，从此再也删不掉。
    #
    # 根因是 N2 的 pop 过早，已修（改为 merge 成功后才从基线摘除）。
    # pop 推迟后，步骤 2 时 a.txt 仍在基线里 → keep 包含它 → 不裁。
    #
    # 这里**保留裁剪**（不改成只增不删）：无限增长的清单会让状态文件
    # 随时间膨胀，而 N45 已在 migrate_state 提供基线全量兜底 ——
    # 即便真的被裁掉，重建基线也能恢复。两害相权取可控的那个。
    #
    # 但裁剪**必须可见**：静默裁掉"曾经拥有过"的记录，会让删除能力
    # 无声消失（这正是 N10 最难排查的地方）。
    _before = seen
    seen = {p for p in seen if p in keep}
    # M13（A14 / A23 实证）：`& set(base)` 让这个判据**恒为空** ——
    # 被裁掉的路径必然不在 `keep` 里，而 `keep` 已经包含 `base` 全集，
    # 于是「不在 keep」与「在 base」互斥，两者交集恒空。
    #
    # 后果：N10 那条最难排查的链路（裁剪让删除能力无声消失）
    # **永远不会有任何提示** —— 保护写了，触发条件不可达
    #（判据 3 的第 7 个实例）。
    #
    # 去掉 & set(base)：所有被裁掉的路径都该报出来。
    # 代价是提示可能变多（本地已删、已出基线的路径也会报），
    # 但那正是"静默失效"要换成的东西 —— 宁可多说，不可无声。
    _cut = _before - seen
    if _cut:
        # M13 修好后 _cut 不再恒空，文案也要跟着改：
        # 原先写"仍在基线里的记录"，而 M13 之后报的恰恰是**已不在基线**
        # 的那批（在 base 里的不会裁）。不改就是新一轮"文案与事实相反"。
        #
        # N382（第一百二十二轮，中危）：裁剪**必须留痕**。
        # 实测链路：误删远端独有的 x.txt → 同一次运行里 x.txt 被裁出
        # local_seen → 事后要回答"它到底是不是本地删的"，证据已经没了。
        # 而下面那句提示还建议 --init-baseline，重建基线会进一步抹掉参照。
        # 误删既没被报告成"已删"，也没留下任何可追溯的痕迹 —— 两处叠加。
        #
        # 留痕只追加、不参与任何判据（纯审计），且上限 200 条防膨胀。
        # N523（第一百七十二轮，低危）：这行过滤用的是 `_path_json_safe`
        # —— 正是判「能否 UTF-8 编码」的**同一个函数**。于是非 UTF-8 路径
        # 进不了 state["local_seen_cut"]；而 `_cut` 本身又只统计「被 keep
        # 裁掉」的那批，非 UTF-8 路径是被**后面**的 `_encodable` 过滤掉的，
        # 也不在 `_cut` 里。
        #
        # 净结果：这类路径既不进 local_seen、也不进 local_seen_cut，
        # `--audit` 里同样查不到。而 M44 的注释写着「静默丢弃正是删除功能
        # 最该说清楚的事」—— 在**提示侧与审计侧两侧都还成立**。
        #
        # 不能编码的路径也无法写进 JSON（这是 state 的落盘格式），
        # 所以不能原样记；但可以记一条**可编码的转义痕迹**，让审计里
        # 至少看得到"有这么一条被丢掉了"。
        _cut_log = []
        for _p in sorted(_cut):
            if _path_json_safe(_p):
                _cut_log.append(_p)
            else:
                _cut_log.append(
                    _p.encode("utf-8", "backslashreplace").decode("ascii"))
        if _cut_log:
            _prev_cut = state.get("local_seen_cut") or []
            state["local_seen_cut"] = sorted(
                set(_prev_cut) | set(_cut_log))[:200]
        print(f"  ! local_seen 裁掉了 {len(_cut)} 条记录"
              f"（{sorted(_cut)[:3]}）—— 这些路径日后若被删除，"
              f"可能因「本地从未拥有过」而不被识别。")
        print("     已记入 state['local_seen_cut']（仅审计，不参与判据）；"
              "要恢复它们可用 --init-baseline，但那会重建基线参照，"
              "误删排查期间请先 --audit 看流水。")
    # 实测链路（PR 工作流）：
    #   1. 删除 a.txt 并推上分支 → 基线 pop 掉 a.txt
    #   2. 推送成功，本函数跑一次 → seen 里 a.txt 被裁掉（它已不在 base/lmap）
    #   3. PR 未 merge 时跑 --pull → 主干上 a.txt 还在 → 写回本地 + 记进基线
    #   4. 用户再次删除 a.txt
    #   5. plan_removals → entries=[] skipped=[]（a.txt 不在 seen 里）
    # 于是「这个文件从此再也删不掉」，且**没有任何提示**。
    #
    # 根因是本字段的语义（"曾经拥有过"）与它的重建来源（lmap = "当前拥有"）
    # 互斥：需要它的时刻正是文件已消失的时刻。裁剪一旦发生就无法自愈。
    #

    # 非 UTF-8 路径必须挡在这里。
    #
    # lmap 的键来自 git（surrogateescape 解码），含非法字节的文件名会带
    # surrogate。混进 local_seen 后 save_state() 的 json.dump 会抛
    # UnicodeEncodeError —— 而那一步在**推送之后**，于是出现「远端改了、
    # 基线没写、报错还说是序列化失败」的三输局面（实测复现）。
    # 这些文件本来也无法经 API 推送（脚本早就过滤并提示改名），
    # 记进 local_seen 没有任何意义。
    good = {p for p in seen if _encodable(p)}
    dropped = len(seen) - len(good)
    # M44（A54）：返回值此前 6/7 个调用点丢弃、report_removals 的
    # skipped_non_utf8 从未被传入 —— 提示是死代码。这里顺手累加进
    # per-run 集合，让那句提示有数据源。
    if dropped:
        global _NON_UTF8_DROPPED
        _NON_UTF8_DROPPED |= (seen - good)
    state["local_seen"] = sorted(good)
    return dropped


# ---------------------------------------------------------------- 识别

def detect_renames(deleted, added, base_shas, local_shas, will_push=None):
    """按 blob sha 把「本地消失」和「本地新增」配对成重命名。

    参数
        deleted    本地已消失的路径列表
        added      本地新增的路径列表
        base_shas  {path: sha} 基线（= 远端上次记录）里的 sha
        local_shas {path: sha} 本地当前 sha

    返回
        (renames, pure_deletes)

    同 sha 的多个新增路径（复制文件时会出现）只消费一个，
    其余仍按新增处理 —— 复制出来的那些本来就该各自独立存在。

    will_push  可选：本次**真的会被推送**的新增路径集合（主流程的 todo）。
                只有落在里面的新路径才算「内容有了新去处」；不在里面的
                配不上 —— 否则「删了旧路径、新路径又被 gitignore 挡掉
                或根本不在待推送列表里」会被当作重命名放行，内容真的丢了
                而报告说这是改名（H1 的实质，不是配对比率本身）。

                **None 表示「不知道」，按不配对处理**（保守）：此时所有
                删除都算真删除，计入熔断。宁可多拦一次，也不能在不知道
                新内容会不会上去的情况下放行 —— 那正是 H1 的原始场景
                （40 删 + 40 同 sha 新增，熔断被静默绕过）。

    **输入先排序**（N5）：这是贪心配对，同 sha 多对多时配对关系随输入
    顺序变化（实测 [o1,o2]→[('o1','n1'),('o2','n2')]，反序则变成
    [('o2','n1'),('o1','n2
    排查「为什么它说我把 a 改成了 b」时非常费解。排序后结果稳定可复现。
    """
    def _pushable(p):
        # None = 不知道哪些会被推送 → 一律不配对（保守，见 docstring）
        return False if will_push is None else (p in will_push)

    added_by_sha = {}
    for p in sorted(added or []):
        s = (local_shas or {}).get(p)
        if s and _pushable(p):
            added_by_sha.setdefault(s, []).append(p)

    renames, pure, used = [], [], set()
    for old in sorted(deleted or []):
        s = (base_shas or {}).get(old)
        cand = [p for p in added_by_sha.get(s, []) if p not in used] if s else []
        if s and cand:
            new = cand[0]
            used.add(new)
            renames.append((old, new))
        else:
            pure.append(old)
    return renames, pure


def collect_removal_candidates(state, lmap):
    """删除候选之一：**已 commit** 的删除。

    local_state_map() 以 git 索引为权威来源，而**未 commit 的删除仍留在
    索引里**（实测：`rm a.txt` 之后 `git ls-files` 照样列出 a.txt，
    `git status` 才显示 " D a.txt"）。所以这一类只能抓到「已 commit 的」。

    未 commit 的删除要靠 git status 的 "D"，由调用方通过 `deleted` 参数
    传入；plan_removals 会把两者取并集。少任何一路都会漏：

      · 只用 git status "D" → 推送成功后脚本自动补 commit，"D" 消失，漏
      · 只用本函数          → 未 commit 的删除还在索引里，抓不到，漏

    返回「基线里有、索引里没了、且本地曾经有过」的路径。第三项是关键：
    它排除了「远端有、本地从来没有过」的文件，否则会把用户从没见过的
    远端内容当成删除。
    """
    base = (state or {}).get("files") or {}
    lmap = lmap or {}
    seen = set((state or {}).get("local_seen") or [])
    cands = [p for p in base if p not in lmap]
    if seen:
        # 不能对 seen 里没有的候选做「基线兜底」：基线包含远端独有的
        # 文件，兜底等于取消判据 3，会把别人的文件当成本地删掉的。
        return sorted(p for p in cands if p in seen)
    # N30：seen 为空时**不能**直接返回空 —— 那样这些路径连 _guard 都
    # 到不了，于是既不删、也不出现在 skipped 里，用户看到「0 个删除」
    # 而根本不知道有东西想删（实测对比：传 deleted 时 entries 有值，
    # 不传时 entries 与 skipped 全空，同一份输入两种结果）。
    #
    # 改为照样返回候选，由 _guard 在判据不可用时**显式拦下并说明**。
    # 安全语义不变（依旧不删），但「没删」这件事变得可见 ——
    # 静默漏删比拦下危险，因为它无法被发现。
    return sorted(cands)


def collect_added(state, lmap):
    """本地新增：lmap 里有、基线里没有。用于重命名配对。"""
    base = (state or {}).get("files") or {}
    return sorted(p for p in (lmap or {}) if p not in base)


# ---------------------------------------------------------------- 决策

def _circuit_break_scope_line(n_pure, total_base):
    """N408：熔断提示里补「其余 M 个文件不受影响」，供用户核对规模。"""
    try:
        rest = int(total_base) - int(n_pure)
    except (TypeError, ValueError):
        return ""
    if rest < 0:
        return ""
    return (f"   本次将删除 {int(n_pure)} 个，其余 {rest} 个文件不受影响"
            f" —— 请核对这个数字是否符合预期（对不上通常是切错 --root"
            f" 或用错基线文件）。\n")


def _circuit_break_raise_hint(n_pure, max_files):
    """N408：放开幅度超过 100 时，把「照抄环境变量」换成先核对再手动改。"""
    target = int(n_pure) + 5
    if target <= 100:
        return f"     PUSH_API_DELETE_MAX_FILES={target} python3 push_api.py ...\n"
    return (f"   本次需提高到 {target}（比默认上限高 {target - int(max_files)}"
            f" 个）—— 幅度过大，不给出可直接照抄的命令：\n"
            f"   请先确认 ① --root 指向的是本仓库 ② 基线文件属于本仓库\n"
            f"   ③ 这些文件确实已从本地删除，再手动设置"
            f" PUSH_API_DELETE_MAX_FILES。\n")


def _circuit_break(n_pure, n_total, total_base, lim):
    """熔断。三道，逐级放宽但都不容无限。

    n_pure   真删除数（不含重命名）
    n_total  删除条目总数（含重命名）—— 这一路是补的，见模块顶部 H1
    """
    if n_pure > lim["max_files"]:
        return True, (
            f"本次要删除 {n_pure} 个文件，超过上限 {lim['max_files']} 个"
            f"（严格超过：正好 {lim['max_files']} 个不触发，"
            f"那时由比例熔断兜底）。\n"
            f"   这么大规模的删除通常是误判（git 读取失败 / 切错 --root / 用错基线文件），\n"
            f"   已中止本次推送。确认无误可用环境变量提高上限：\n"
            f"   （重命名不计入此数，故改名大量文件不会触发。）\n"
            # N408（第一百三十一轮，中危）：提示直接算出具体数值让用户照抄，
            # 而截图的场景（一次清理 1197 个遗留文件）恰恰是「看起来就该
            # 批量删」的操作 —— 照抄后熔断这道防线等于没了。
            #
            # 补两条：① 本次将删 N 个、**其余 M 个不受影响**（让用户核对
            # M 是否符合预期 —— 数字对不上就说明切错 --root / 用错基线）；
            # ② 放开超过 100 时要求二次确认。
            + _circuit_break_scope_line(n_pure, total_base)
            + _circuit_break_raise_hint(n_pure, lim["max_files"])
        )
    # M7（A12 / A23 收窄）：不是"删 90% 零熔断"，而是
    # **total_base < ratio_min_files(20) 时比例判据完全不启用**。
    # wipe_min_files=5 只拦住了"删光"，拦不住"删 90%"：
    #   19 个文件的仓库删 17 个 → 89% 远超熔断线，但不满 20 → 比例判据跳过
    #   且 17 < 19 不是"删空" → 删空判据也跳过 → **零拦截**
    # 而这正是 docstring 声称要防的"批量误判"里最典型的形态。
    #
    # 不能简单地把 ratio_min_files 调小：小仓库删 1 个就占 50%，
    # 一刀切会把正常操作全拦掉（那才是 N25 加这个下限的原因）。
    #
    # 破局点：门槛不该只看**分母**（仓库多大），还要看**分子**
    #（要删多少个）。删 17 个无论仓库多大都是"批量"，
    # 删 1 个无论仓库多小都是"单个操作"。
    # 所以启用条件改成"分母够大 **或** 分子够多"。
    _ratio_on = (total_base >= lim["ratio_min_files"]
                 or n_pure >= lim.get("ratio_min_pure",
                                      DELETE_RATIO_MIN_PURE))
    if _ratio_on and n_pure / total_base > lim["max_ratio"]:
        return True, (
            f"本次要删除 {n_pure} 个文件，占基线文件总数 {total_base} 的 "
            f"{n_pure / total_base:.1%}，超过 {lim['max_ratio']:.0%} 熔断线，已中止。\n"
            f"   确认无误可用环境变量提高比例：PUSH_API_DELETE_MAX_RATIO=0.5\n"
            f"   比例判据的启用条件：基线 ≥ {lim['ratio_min_files']} 个文件，"
            # N358（第一百一十二轮，低危）：这一句原本插的是
            # **常量** `DELETE_RATIO_MIN_PURE`，而上面（16833 行）的判据取
            # `lim.get("ratio_min_pure", …)` —— 环境变量改得动判据、
            # 改不动提示。
            #
            # 实测（PUSH_API_DELETE_RATIO_MIN_PURE=3，total_base=10、
            # n_pure=4）：判据按 3 → 4 个就熔断；提示仍写「≥ 10 个」。
            # 用户读到的是**与自己设的值相反**的门槛，会以为环境变量没生效。
            #
            # 同族：N270 / N325 是"读进 lim 却零引用"（完全不生效），
            # 这条是"生效了但说的还是旧值"。方向相反更隐蔽 —— 值确实改了，
            # 只有话没跟着改。
            #
            # 修法：与判据取同一个 `lim`（连默认值一起对齐），
            # 不再出现第二份口径。
            f"**或**本次真删除 ≥ {lim.get('ratio_min_pure', DELETE_RATIO_MIN_PURE)} 个（M7）——\n"
            f"   前者为小仓库留余地（删 1 个就占 50% 属正常），"
            f"后者堵住小仓库批量删除的缝隙。"
            # M197（A167，中危）：这个数字**同时**是关闭开关。判据是 OR，
            # 把它调大到本次真删除数之上，第二条就不启用；在
            # total_base < ratio_min_files 的小仓库里，比例熔断整体失效
            # （实测 80% 批量删除零熔断）。而这句提示只描述「启用条件」，
            # 读起来像加严旋钮 —— 调完之后提示**再也不会出现**，用户
            # 拿不到任何反馈说自己把熔断关了。
            f"\n   注意：把它调**大**是放宽 —— 达到该数量的删除才会走"
            f"比例熔断；调过头会让这条判据对小仓库完全失效（M197）。"
        )
    # N39：小仓库的整仓删除原先**完全没有保护**。
    # 绝对熔断要求 n_pure > max_files(20)，比例熔断要求
    # total_base >= ratio_min_files(20) —— 于是一个只有 19 个文件的仓库
    # 把 19 个全删掉：19 不大于 20，比例熔断又不启用，**零拦截**。
    #
    # 讽刺的是 docstring 明写熔断就是为拦「把整个仓库都消失了当成一次
    # 正常删除」，而「小仓库整仓删除」恰恰是那一类里最典型的形态。
    #
    # 补一条与仓库规模无关的判据：**删光了**本身就是最强信号。
    # 正常操作不会一次删掉基线的 100%（那是重建仓库，不是删文件）。
    # 下限（DELETE_WIPE_MIN_FILES）：小仓库全删是正常操作，
    # 不该被「删空」判据连带拦掉 —— 实测 2 个文件的仓库全删会被误伤。
    # 熔断要防的是「批量误判」，两三个文件不存在误判空间。
    _wipe_min = lim.get("wipe_min_files", DELETE_WIPE_MIN_FILES)
    if total_base > 0 and n_pure >= total_base and n_pure >= _wipe_min:
        return True, (
            f"本次要删除 {n_pure} 个文件，而基线总共只有 {total_base} 个 —— "
            f"等于把整个仓库删空。\n"
            f"   这通常意味着 git 读取失败 / 切错 --root / 用错基线文件。\n"
            # N109：给出的出路是 MAX_FILES，但**这条判据根本不看它**
            #（判据是 n_pure vs total_base vs wipe_min）。
            # 用户照着调 MAX_FILES，熔断照拦不误，且没有任何解释。
            # 真正能调的是 WIPE_MIN（本判据的规模下限）。
            f"   确实要删空请显式提高本判据的规模下限：\n"
            f"     PUSH_API_DELETE_WIPE_MIN={n_pure + 5} python3 push_api.py ...\n"
            f"   （此判据与仓库大小无关：小仓库整仓删除此前无任何熔断保护。）"
        )

    # 第三道：删除条目**总数**的绝对上限。
    #
    # 为什么不用「占基线比例」（H1 给的比例方案）：比例判据会误杀合法的
    # 全量改名 —— 25 个文件各自改名，n_total/total_base = 100%，
    # 但内容一个字节都没丢，拦下它是纯误伤（实测复现）。
    #
    # 真正的风险不是「改动路径占多大比例」，而是「删掉的内容有没有新去处」。
    # 那条已由 detect_renames 的 will_push 判据处理：新路径不会被推送时
    # 配不上对，自动降级成真删除并计入 n_pure，前两道自然会拦。
    #
    # 这一道只兜「数量大到不可能是手工操作」的极端情况，用绝对数。
    # N122：绝对数判据**同样会误杀全量改名**，只是把门槛从"比例"
    # 换成了"个数"。实测：70 个文件整体改名（内容不变、n_pure=0，
    # 一个字节没丢）被拦下 —— 与上面那段注释的承诺直接矛盾。
    #
    # 关键区别在 n_pure：
    #   · 全量改名 → 新路径都有去处，n_pure == 0
    #   · 真批量删除 → n_pure 大
    # 内容没有新去处的场景已由第一、二道（n_pure）覆盖。
    # 所以这一道只在 **确实有真删除** 时才生效，纯改名不该被它拦。
    # N325：这里原先直接用常量 DELETE_TOTAL_FACTOR，于是
    # PUSH_API_DELETE_TOTAL_FACTOR **设了完全没用**（读进 lim 却零引用）
    # —— 在"阈值可用环境变量覆盖"的 docstring 下列了个死旋钮。
    # 改成读 lim（它已由环境变量覆盖），并让下面所有引用跟着走。
    _tf = lim.get("total_factor") or DELETE_TOTAL_FACTOR
    cap = lim["max_files"] * _tf
    if n_total > cap and n_pure > 0:
        return True, (
            f"本次要删除 {n_total} 个远端路径（其中重命名 {n_total - n_pure} 个），"
            f"超过上限 {cap} 个（= 真删除上限 {lim['max_files']} × "
            f"{_tf}），已中止。\n"
            f"   这个量级通常不是手工改名，更像「git 读取失败 / 切错 --root /\n"
            f"   用错基线文件」导致的整仓平移。确认无误：\n"
            f"     PUSH_API_DELETE_MAX_FILES={n_total // _tf + 5} "
            f"python3 push_api.py ..."
        )
    return False, ""


def _as_path_set(v):
    """把基线字段安全地转成路径集合。**对污染形态零容忍**。

    N23 实测：`local_seen` 被写成了字符串 "abc.txt"（旧版迁移 / 手工编辑
    都可能造成），`set("abc.txt")` 得到 {'a','b','c','.','t','x'} ——
    于是判据 3「本地曾经拥有过」拿一堆字符去比对，任何真实路径都不在
    其中，**删除静默失效**，而这份被拆开的脏数据还会被 save_state 原样
    持久化写回，下次照旧失效。

    字符串必须被**识别为污染**而不是当成可迭代对象展开：
    Python 里 str 是最容易被误当成序列的类型，展开它不报错、不告警，
    正是「静默失败」的典型形态。
    """
    if v is None:
        return set()
    if isinstance(v, str):
        return set()          # 污染：宁可判据失效（漏删），不能误删
    if isinstance(v, dict):
        return set(v.keys())
    if isinstance(v, (list, tuple, set, frozenset)):
        return set(x for x in v if isinstance(x, str))
    return set()


#: N454 / N456（第一百四十九轮，中危）：**逐路径**记录「查不到」的事实。
#:
#: N456：`plan_removals` 的 skipped 原因恒写「本地仍存在」，而 `is_there`
#: 在 EACCES / ENAMETOOLONG 下返回 True 的真实含义是「**无法判断**」。
#: 同一份输出里详细提示说「无法判断」、skipped 说「本地仍存在」，
#: 两处真话互相否定。用户看到「本地仍存在」第一反应是去 ls 那个文件
#: —— 而它读不到（正是这类 errno 的常见形态），于是要么以为脚本判错、
#: 要么以为自己记错，真正的原因被假归因盖掉。
#:
#: 只有把「无法判断」这个事实从 `_local_exists` 传出来，两处才能同源。
_LOCAL_EXISTS_UNKNOWN = {}
_LOCAL_EXISTS_WARNED = set()


def _local_exists(rel):
    """本地是否存在该路径 —— 删除判据的**唯一**口径（M17 / M19）。

    `os.path.exists()` 对**悬空软链返回 False** —— 链接本身在，
    只是目标没了。于是同一份事实被判成两个相反结论：

    | 场景 | 后果 | 方向 |
    |---|---|---|
    | 悬空软链不进 `local_seen`（M17） | 日后删它时静默漏删 | 安全侧 |
    | 悬空软链被判"本地已删"（M19） | **误删远端** | 危险侧 |

    `plan_removals` 的 docstring 自述定位是"宁可漏删不可误删"：
    判据选对了（存在性必须用文件系统判定），
    但选的函数对悬空软链**撒谎**。

    `os.path.lexists()` 才是"链接本身在不在"。

    ⚠ 三处调用点原先是三份逐字相同的 lambda，
    改一处不等于改三处 —— 必须收敛成一个函数。
    """
    # N364（第一百一十四轮，中危）：上面那个 `except` 是**死代码** ——
    # `os.path.lexists` 内部已捕获 `(OSError, ValueError)` 并返回 False，
    # 从不向外抛。实测：
    #     NUL 字节      → False（未抛）
    #     超长名(5000)  → False（未抛）
    #
    # 于是真正生效的不是那个 except，而是"静默返回 False"这条路径 ——
    # 而 False 的含义是**「本地没有了」= 放行删除**。注释写着"向安全侧
    # 倒"，实际倒向了危险侧。
    #
    # 三处问题叠在一起：
    #   ① 方向反了：`_guard_exists`（紧邻的下一个函数）对同一类失败
    #      返回 True（判成"还在"→ 不删，fail-closed）。
    #   ② except 不可达（见上）。
    #   ③ 它让 `_guard_exists` 整体失效 —— 那个包装器的判据是"回调
    #      **抛异常**时返回 True"，而这里在内部把异常吞掉、转成 False
    #      返回，包装器看到的是一次正常返回，保护无从触发。
    #
    # 端到端实证（构造绝对路径 > PATH_MAX 的深目录树，文件真实存在）：
    #     绝对路径长度 4241、叶目录下 stat 'leaf.txt' → True、内容在
    #     ★ _local_exists(rel)  → False    ← 应为 True
    #     ★ plan_removals       → 1 条删除 entries，skipped 为空
    #
    # 即：**本地文件确实存在，却被判「本地已删」，生成了远端删除条目。**
    # 同类触发（无需极端深度）：父目录 chmod 000 后以非 root 身份运行，
    #     root 下 lexists=True / nobody 下 lexists=False —— 文件在，读不到。
    #
    # 修法：显式 lstat，区分「真不存在」与「查不到」。
    # 只有 FileNotFoundError / NotADirectoryError 才算"确实没有"，
    # 其余 OSError（EACCES 权限、ENAMETOOLONG、挂载异常…）一律按
    # 「还在」处理 —— 不删。这与 `_guard_exists` 的口径一致。
    try:
        os.lstat(os.path.join(ROOT, rel))
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False                 # 确实没有
    except OSError as _e:
        # 查不到 ≠ 没有。按「还在」处理，不删。
        _LOCAL_EXISTS_UNKNOWN[rel] = type(_e).__name__
        # N454（第一百四十九轮，中危）：原先去重键是**一个进程级 bool**，
        # 于是 3 个文件查不到（同一个 errno）时只提示 1 个 —— 而提示里
        # 带的是那个文件的名字，读起来就是"只有它"。注释给的去重理由是
        # 「避免刷屏（同一次运行里原因都一样）」：原因一样不等于文件
        # 只有一个。改为按 **errno 类型**去重，并在末尾汇总受影响总数。
        _k = type(_e).__name__
        if _k not in _LOCAL_EXISTS_WARNED:
            _LOCAL_EXISTS_WARNED.add(_k)
            # rel 本身可能就有几千字符（ENAMETOOLONG 正是这个场景），
            # 整条打出来会把真正的错误提示冲掉。
            _r = str(rel)
            _r = _r if len(_r) <= 60 else _r[:57] + "..."
            print(f"  ! 无法判断 {_r!r} 是否存在"
                  f"（{_k}）——按「还在」处理，本次不删它。")
        return True
    except ValueError as _e:
        # 路径含 NUL 等：同样是"查不到"，同上按「还在」处理。
        _LOCAL_EXISTS_UNKNOWN[rel] = type(_e).__name__
        _k = type(_e).__name__
        if _k not in _LOCAL_EXISTS_WARNED:
            _LOCAL_EXISTS_WARNED.add(_k)
            _r = str(rel)
            _r = _r if len(_r) <= 60 else _r[:57] + "..."
            print(f"  ! 无法判断 {_r!r} 是否存在"
                  f"（{_k}）——按「还在」处理，本次不删它。")
        return True


def _guard_exists(exists):
    """给 exists 回调包一层异常保护（N15）。

    调用方传的是 `lambda p: os.path.exists(os.path.join(ROOT, p))`。
    路径含 NUL 字节、文件名过长、权限异常时 os.path.exists 会抛
    ValueError / OSError —— 而它在 `for p in pure` 的循环里裸调，
    一个坏路径就让**整个 plan_removals 抛出**，删除计算整体中断。

    按 fail-closed 处理：查不了存在性 = 不知道本地有没有 = 不删。
    这与模块「宁可漏删不可误删」的定位一致。
    """
    def _wrapped(p):
        try:
            return bool(exists(p))
        except Exception:
            return True        # 判成「还在」→ 不删（fail-closed）
    return _wrapped


def _ignored_untracked_shas(limit=5000):
    """被 .gitignore 屏蔽的未跟踪路径 → 内容指纹（blob sha）。

    N634（A120 §3.4，中危）：`plan_removals` 里那套「新位置被屏蔽」的
    检测（`shadowed`）**实测一次都不触发**，本函数是让它真正有数据来源。

    为什么不触发：`shadowed` 靠 `_added_raw` 里"被 `ignore()` 剔除的路径"
    去找新位置，而 `added` 来自 `collect_added(state, lmap)`，`lmap` 的两个
    来源（`git ls-files -s` = 已跟踪；`git status --porcelain -uall` =
    未跟踪但**未被忽略**）**都不可能含被 .gitignore 屏蔽的路径**。

    实测（本轮复现，与 A120 §3.2 同一构造：`mv old.txt new.txt`、
    `.gitignore` 里写着 `new.txt`）：
        lmap 键            = ['.gitignore', 'keep.txt']   ← new.txt 不在
        gitignored_set(键) = set()                        ← 恒空
        → _dropped_new 恒空 → shadowed 恒空
    也就是说「改名到被屏蔽路径 → 内容从远端消失」这句"已修"是**假的**：
    代码写得对，但它要找的东西从来没进过它能看到的集合。

    git 有一条专门的查询能列出被忽略的未跟踪文件：
        git ls-files --others --ignored --exclude-standard
    （A120 实测能列出 new.txt；本文件里 `--ignored` 此前出现 **0 次**。）

    sha 用 `git hash-object --stdin-paths` 一次算完：逐个调用在
    node_modules 这类目录下是上万次子进程，而 `--stdin-paths` 走 stdin，
    既不撞 ARG_MAX 也只有一个进程。
    """
    try:
        out = git("ls-files", "--others", "--ignored",
                  "--exclude-standard", "-z")
    except SystemExit:
        return {}
    paths = [p for p in (out or "").split("\0") if p]
    if not paths:
        return {}
    if limit and len(paths) > limit:
        # 超上限时**如实截断**：不假装全查了。改名目标通常不在最末尾，
        # 且这条只用于"内容是否有别的去处"的告知，漏报方向是安全的
        # （漏报=不点名，回到修之前的行为；不会误删）。
        paths = paths[:limit]
    try:
        proc = subprocess.run(["git", "-C", ROOT, "hash-object", "--no-filters",
                               "--stdin-paths"], capture_output=True, text=True,
                              errors="surrogateescape",
                              input="\n".join(paths),
                              timeout=DEFAULT_GIT_TIMEOUT)
        shas = [ln.strip() for ln in (proc.stdout or "").splitlines()]
    except (OSError, subprocess.SubprocessError):
        return {}
    if len(shas) != len(paths):
        return {}
    return {p: s for p, s in zip(paths, shas) if p and s}


def plan_removals(state, rstate, lmap, deleted=None, added=None,
                  force=False, gitignored=None, exists=None,
                  submodules=None, will_push=None, allowed=None):
    """算出本次提交要带的删除条目。**不直接改任何东西**，只返回决策。

    N119：`will_push` 是实际参数，docstring 却零提及 —— 而它是
    **重命名识别的开关**：默认 None 时 `_pushable()` 一律返回 False，
    重命名一个都配不上对，全部降级成真删除。
    于是「整仓改名」被判成「整仓删除」并触发熔断，而提示完全不提
    判据来自 will_push，用户无从下手。
    调用方必须显式传，否则等于关掉了重命名识别。

    参数
    --------
        state       push_api 的基线 dict（含 files / local_seen / conflicts）
        rstate      {path: (mode, sha)} 远端当前状态
        lmap        {path: (mode, sha)} 本地当前状态。
                ⚠ 取自 git **索引**（local_state_map），不是工作区：
                已暂存的改动算在内，未暂存的编辑不算（N6：原注释自称
                「工作区状态」与实现不符，会让人误判它能看到未 add 的改动）。
                删除判定不靠它（那一路走 exists 回调查文件系统）。
        allowed      点名推送的范围（N370）。None = 不过滤（默认，行为不变）。
                     非 None 时，删除条目**只保留**在范围内的路径 ——
                     即使下面「两路并集」把 committed 补回来，也照样被挡。

                     为什么必须再传一次：本函数内部会无条件做
                     `merged = list(deleted); merged.extend(committed)`，
                     于是调用方传 `deleted=[]`（已经按点名范围过滤过）
                     时，committed 会把点名范围外的删除**整条加回来**。
                     主流程那一层的过滤对它无效 —— N370 的第一版修复
                     就是这么失败的（实测 `deleted=[]` 而 entries=['b.txt']）。

        deleted     调用方从 git status 取到的 "D" 路径。**强烈建议传**：
                    未 commit 的删除仍在索引里，本模块自己推不出来。
                    省略时只剩「已 commit 的删除」这一路，会漏。
        added       新增路径列表；省略则自行推导（调 collect_added），
                    **不会为空** —— 线上三个调用点都没传，功能正常。
                    （N14：原 docstring 写「务必让它有值，否则配对恒空」，
                     与实现矛盾，会误导后来者以为必须显式传。真实约束是：
                     确实没有新增时配对才为空，那是正确结果。）
                    显式传可用于让调用方控制配对候选集。
                    ⚠ 传入后会被**过滤**（N14：原 docstring 没说）：
                    被 .gitignore 屏蔽的、以及非 UTF-8 的路径会被剔除，
                    因为主流程不会推送它们 —— 若拿未过滤的清单配对，
                    会出现「删旧 + 新内容没上去」= 内容真的丢了，
                    而报告却说这是重命名。
        force       是否放行 --force-overwrite / --force-delete。
                    ⚠ 它**不绕过删除熔断**（N409）：熔断只看规模
                    （条目数 / 占比），是独立的一道判据，force 与否都照样
                    拦（实测 force=True + 30 个真删除 → abort、entries 清空）。
                    三者关系：--force-overwrite 放行「覆盖他人改动」；
                    --force-delete 放行删除侧的判据；熔断放行不了。
        gitignored  可选：判断路径是否被 .gitignore 屏蔽的 callable。
                    不传则用保守默认（只挡 .git/ 与目录项）。
        exists      判断路径在当前工作区是否还存在的 callable。
                    **必须传**，不传则**拒绝删除并报告原因**（L5）。
                    旧版静默退回 `path in lmap`，而 lmap 取自 git 索引、
                    里面残留着未 commit 的已删文件（见
                    collect_removal_candidates），于是这类删除被判成
                    「本地仍在」而漏掉 —— 漏删虽比误删安全，但它是
                    **静默**的，用户以为删了、远端其实还在。
                    与 docstring「存在性必须用文件系统判定」自相矛盾的
                    默认值，本身就是个契约陷阱，去掉它。
        submodules  可选：远端 submodule 路径集合。这些路径在 rstate 里
                    取不到（tree API 里 type=commit 被 blob 过滤），
                    「远端被他人改动」闸门对它们恒失效（N1），必须显式
                    排除，否则 submodule 指向的 commit 被更新后检测不到。
    返回
        {"entries": [...], "renames": [...], "skipped": [(path, 原因)],
         "abort": bool, "abort_reason": str, ...}
    """
    state = state or {}
    base_files = state.get("files") or {}
    lmap = lmap or {}
    rstate = rstate or {}
    submodules = submodules or set()

    base_shas = {p: _entry_sha(v) for p, v in base_files.items()}
    local_shas = {p: s for p, (_m, s) in lmap.items()}
    # N490：`local_shas` 把 `_m` 丢了，于是 `_guard_target` 拿不到本地 mode。
    # 与上面同一份 lmap、同一行推导，避免再长出第二份口径。
    local_modes = {p: m for p, (m, _s) in lmap.items()}

    # 两路并集：调用方给的 git status "D" + 本模块推的「已 commit 的删除」。
    committed = collect_removal_candidates(state, lmap)
    if deleted is None:
        deleted = list(committed)
        derived = True
    else:
        derived = False
        # 去重（L4）：重复路径会让 entries 出现两条同 path 条目，
        # 还会让熔断计数虚高 —— 用户什么都没做却被拦。
        # N170 / M8：原名 `seen_del` 从未被写入（全函数无 seen_del.add），
        # 于是 `if p not in seen_del` 恒为真 —— 去重实际由下一行
        # dict.fromkeys 完成。
        #
        # 死变量本身已被 N170 的注释标注，但**代码还在**。留着它会让人
        # 以为过滤生效了（注释说了不算，代码里有个变量在那儿）。
        # 这里连同判据一起删掉：去重由 dict.fromkeys 一处完成。
        merged = list(deleted)
        merged.extend(committed)
        deleted = list(dict.fromkeys(merged))

    if added is None:
        added = collect_added(state, lmap)

    # N40：`gitignored or _default_gitignored` 的写法让**传了就绕过兜底**。
    # 实测：gitignored_set() 在 git 不可用 / 超时时返回空集（fail-open），
    # 于是调用方传来的是「恒 False」的 callable —— 内置兜底（挡 .git/
    # 与目录项）因此完全失效，`.git/config` 被判成待删除。
    #
    # 兜底拦的是**无论 git 怎么说都必须挡**的那一类（仓库元数据、
    # 目录项），它不该被调用方的失败降级掉。改成无条件叠加。
    _caller_ignore = gitignored

    def ignore(p):
        if _default_gitignored(p):
            return True
        if _caller_ignore is None:
            return False
        try:
            return bool(_caller_ignore(p))
        except Exception:
            # 回调自身出错时也按「忽略」处理：宁可漏掉一个待删项，
            # 也不能放行 .git/ 这类路径（漏删安全，误删不可逆）。
            return True
    # N8：非 UTF-8 路径（surrogate）必须在这里挡掉。
    #
    # 同一个模块里 `sync_local_seen` 对它有完整防护（还配了长注释解释
    # 后果是「远端改了、基线没写、报错还说是序列化失败」的三输局面），
    # 而 deleted 一侧**此前完全没有对应过滤** —— 于是 surrogate 路径
    # 正常生成删除条目，一路走到 `json.dumps(...).encode("utf-8")` 才抛
    # UnicodeEncodeError。崩溃发生在**请求体编码阶段**，报错说的是序列化
    # 失败，看不出是文件名的问题。
    #
    # 关键点：`exists` 回调对 surrogate 路径返回 False 且**不抛异常**
    # （实测），所以「本地仍存在」那道闸门拦不住它 —— 它会被正常判为
    # 待删除。防护只能做在这里。
    _bad_enc = [p for p in deleted if p and not _encodable(p)]
    if _bad_enc:
        deleted = [p for p in deleted if p and _encodable(p)]
        global _NON_UTF8_DROPPED
        _NON_UTF8_DROPPED |= set(_bad_enc)
    deleted = [p for p in deleted if p and not ignore(p) and _safe_rel(p)]
    # N370：点名范围。**必须放在上面「两路并集」之后** —— 那一句会
    # 无条件把 committed 补回来，放在它之前等于没过滤。
    if allowed is not None:
        _al = set(allowed)
        deleted = [p for p in deleted if p in _al]
    # 越界路径要在**这里**挡掉，不能只靠调用方（M3）。
    #
    # os.path.join(ROOT, "/etc/x") 会丢弃 ROOT 直接得到 "/etc/x"，
    # os.path.join(ROOT, "../x") 会穿到仓库外。主流程目前喂的都是相对
    # 路径所以碰不到，但删除是**不可逆**操作，模块不能把安全寄托在
    # 「调用方恰好没传错」上 —— 那是隐式契约，改一行就会破。
    # 实测：传入 "/etc/nonexistent" 与 "../outside.txt" 都会生成删除条目。
    # added 同样要过滤（M1）：新增路径若被 gitignore 屏蔽，主流程不会推送它，
    # 于是「删旧 + 新内容没上去」= 内容真的丢了，报告却说这是重命名。
    #
    # N22（第六轮，中危，**实测仍未修**）：这里**曾经**只滤 gitignore，
    # 不滤可编码性 —— 于是含 surrogate 的新路径照样进 `added_by_sha`，
    # 与旧路径配成重命名，`renames` 里就带着一个**永远推不上去**的
    # 新路径（tree API 的请求体也要 UTF-8）。
    #
    # 双重后果：
    #   ① 报告说「内容改名到了 X」，而 X 根本到不了远端 —— 正是 H1 那类
    #      「删旧 + 新内容没上去 = 内容真的丢了」，只是伪装成了重命名。
    #   ② 打印时按 stdout 的 error handler 分叉：surrogateescape 下输出
    #      乱码、`utf-8:strict` 下直接 `UnicodeEncodeError` 崩溃
    #      （实测复现，栈停在 report_removals 的 ≈ 行）。
    #
    # 与 N8（deleted 侧）对称：两路都不能带不可编码路径。
    added_orig = list(added or [])
    added = [p for p in added if p and not ignore(p) and _path_json_safe(p)]
    # N598（A155，中）：added 侧过滤了却**不登记**，而 20 行之外的
    # deleted 侧（N8）是 `global _NON_UTF8_DROPPED` 并 `|=` 的。
    #
    # 两侧写法不对称的后果：「你改名到的那个文件因为文件名推不上去」
    # 这件事**全程零告知** —— `_NON_UTF8_DROPPED` 恒空，于是
    # `report_removals` 那句「N 个非 UTF-8 路径已排除」永远是 0，
    # 用户看到的是「将删除 1 个远端文件」，不知道内容就此消失。
    #
    # 与 deleted 侧同口径登记，告知才有数据源。
    _added_bad = [p for p in added_orig
                  if p and not _path_json_safe(p)]
    if _added_bad:
        # 不再重复 `global`：本函数 deleted 侧（N8）已声明过，同一作用域
        # 内第二次声明会让 Python 报「assigned to before global declaration」
        # （该名字在两处之间已被赋值）。一处声明即覆盖全函数。
        _NON_UTF8_DROPPED |= set(_added_bad)

    # N489（A121，中危）：被 `ignore()` / 非 UTF-8 剔除的新路径**就此消失**，
    # 而它们正是「旧路径的内容本该去的地方」。配不上对之后，旧路径落进
    # `pure`、按普通真删除处置 —— 删除照样发生，但**这份内容在远端从此
    # 没有新去处**。
    #
    # 实测（同一操作 `mv old.txt new.txt`，唯一变量是 new.txt 是否被
    # .gitignore 屏蔽）：
    #   未屏蔽 → 报「≈ old.txt → new.txt（重命名）」，远端 new.txt 有内容
    #   屏蔽   → 报「将删除 1 个远端文件」，old.txt 已删、new.txt 无
    #           ★ 内容从远端彻底消失，全程**没有任何一处**提到被屏蔽
    #
    # 而 `detect_renames` 的 docstring 顶部 H1 原文写着的正是「删了旧路径、
    # 新路径又被 gitignore 挡掉……会被当作重命名放行，内容真的丢了」
    # —— 配对这一半修好了（will_push 判据），**告知这一半没有**。
    #
    # 保留剔除后的清单，供下面把「被屏蔽的新去处」认出来并点名。
    _added_raw = [p for p in (added_orig or []) if p]

    if exists is None:
        # fail-closed：拿不到可信的存在性判据就不删，并说清原因。
        return {
            "entries": [], "renames": [], "skipped": [],
            "abort": True,
            "abort_reason": (
                "调用方未提供 exists 回调，无法判定「本地是否真的没有了」，"
                "已拒绝删除。\n"
                "   存在性必须查文件系统：git 索引里残留着未 commit 的已删"
                "文件，\n   用它判断会把真删除误判成「本地仍在」而漏掉。"),
            "derived_deletes": False,
            "pure_delete_count": 0, "total_delete_count": 0,
            "intended_pure_count": 0, "intended_total_count": 0,
        }
    is_there = _guard_exists(exists)
    seen_set = _as_path_set(state.get("local_seen"))
    conflicts = _as_path_set(state.get("conflicts"))

    renames, pure = detect_renames(deleted, added, base_shas, local_shas,
                                  will_push=will_push)

    # N489（接上）：把「旧路径的 base sha == 某个被屏蔽的新增路径的 sha」
    # 认出来 —— 那就是「内容有本地新位置，但那个位置不会上传」。
    # 它此刻在 `pure` 里（配不上对），会照常生成删除条目；若不点名，
    # 用户看到的只是「将删除 1 个远端文件：old.txt」—— 这句话是真的，
    # 但没说**这份内容就此从远端消失**。
    shadowed = []
    # N598（A155，中）补完：`_dropped_new` 原先写的是
    # `... and _path_json_safe(p)` —— 不可编码的新路径**被排除在外**，
    # 于是「内容没有远端副本」这句点名只对 .gitignore 屏蔽生效，
    # 对 N598 的主场景（新文件名含 surrogate）**恰好不生效**。
    # 排除它的原因可以理解：下面 `print` 时裸打 surrogate 会崩
    # （正是 N22 那条）—— 但正确做法是显示侧走 `_disp_path`，
    # 而不是把这类路径从检测里剔掉。两者都要：检测用真实路径，显示用转义。
    _dropped_new = [p for p in _added_raw if p not in added]
    if _dropped_new or pure:
        _new_by_sha = {}
        for p in _dropped_new:
            s = local_shas.get(p)
            if s:
                _new_by_sha.setdefault(s, p)
        # N634（接上）：`_dropped_new` 恒空（见 `_ignored_untracked_shas`
        # 的说明），真正的来源是被 .gitignore 屏蔽的未跟踪路径 ——
        # 只在 `pure` 非空（确实有删除）时才去查，避免每次都跑一次 git。
        if pure:
            for _ip, _is_ in _ignored_untracked_shas().items():
                if _is_:
                    _new_by_sha.setdefault(_is_, _ip)
        for _old in pure:
            _s = base_shas.get(_old)
            if _s and _s in _new_by_sha:
                shadowed.append((_old, _new_by_sha[_s]))

    # N30：两路信号对同一输入给出**相反结果**，且 committed 路静默漏删。
    # 实测（local_seen 为空——老基线或被污染）：
    #   deleted=["z.txt"] 传入 → entries=['z.txt']   （deleted 路不查 seen）
    #   不传 deleted          → entries=[] skipped=[]（committed 路要求 p in seen）
    #
    # H3 修的是前一半（把判据 3 下沉到 _guard），本条修后一半：
    # 判据 3 在缺失 local_seen 时会把 committed 路的候选全部挡掉，
    # 而它们**连 skipped 都不进** —— 用户看到「0 个删除」，
    # 既不知道有东西想删、也不知道为什么没删。
    #
    # 做法：seen_set 为空（判据不可用）时，把因此被挡掉的路径记进
    # skipped 并给出可读原因 —— 静默漏删比拦下危险，因为它不可见。
    _seen_available = bool(seen_set)

    entries, skipped = [], []
    # N8：上面挡掉的非 UTF-8 路径要**说清**，不能静默丢弃 ——
    # 用户会以为那个文件已经在删除清单里（远端已经删了）。
    for _bp in _bad_enc:
        skipped.append((_bp, "文件名含非 UTF-8 字节（surrogate），"
                            "无法经 API 推送，请改成 UTF-8 文件名后再删"))

    # N623（第二百〇五轮，低危）：`new_path` 形参已删 —— 函数体内零引用，
    # 而调用点 `_guard(old, new)` 把重命名目标传了进来，读代码的人会以为
    # `_guard` 已经管了目标路径；真正的目标校验是紧邻的 `_guard_target(new)`。
    # 当前行为是对的，但陷阱成立：日后删掉 `_guard_target` 那一行、或新增
    # 调用点只写 `_guard(old, new)`，校验就没了 —— 而死形参不会报错。
    def _guard(path):
        """统一的删除前置校验。返回 (ok, 原因)。

        两路（调用方传入 / 本模块推导）共用这一套判据。判据不能分散：
        `collect_removal_candidates` 里查的 local_seen 只覆盖它自己那一路，
        调用方传入的路径会绕过（H3，实测复现）。
        """
        _rm, rsha = rstate.get(path, (None, None))
        b_sha = base_shas.get(path)
        b_mode = _entry_mode(base_files.get(path))

        # 存在性必须用文件系统判定，不能用 lmap：未 commit 的删除仍在
        # git 索引里，`path in lmap` 会把它判成「本地还在」而拒绝删除。
        if is_there(path):
            # N456：原因必须与 `_local_exists` 的实际判定同源。
            # `is_there` 返回 True 有两种截然不同的含义：
            #   · 确实在            → 「本地仍存在」是真话
            #   · 查不到（EACCES…） → 同样是 True，但「本地仍存在」是假的
            # 后者会指引用户去 ls 一个读不到的文件。按「无法判断」措辞。
            _why_not = _LOCAL_EXISTS_UNKNOWN.get(path)
            if _why_not:
                return False, (f"无法判断是否存在（{_why_not}）"
                               f"——按「还在」处理，本次不删它")
            return False, "本地仍存在，判定为误报，不删"

        # N61：`MD` 状态（暂存了新内容、随后工作区删除）是个陷阱。
        # 实测：echo new > x && git add x && rm x → status 给 "MD"，
        # 而 ls-files 里 x 的 sha 是**新内容**。
        #   · collect_removal_candidates：索引里还有 → 不算删除
        #   · _detect_deletions：xy="MD" 含 D → 算删除
        # 两路冲突，并集按弱信号（"算删除"）处理 → 删掉远端文件，
        # 而索引里暂存的新内容**永远不会被推送**（它不在 todo 里）。
        # 净效果：内容在远端消失、在本地也只存在于索引中 —— 真的丢了。
        #
        # 判据：该路径在本地索引里有内容、且索引内容与基线不同
        # （= 有未推送的暂存改动），此时不能删。
        if path in local_shas and local_shas[path] != b_sha:
            # N161：这里把 **AD** 也报成 MD。
            # 两者处置含义不同：
            #   MD = 暂存后又被删除（有一份工具没见过的内容会随删除消失）
            #   AD = 新建后删除、从未推送（远端根本没有它，删了不丢东西）
            # 结论都是"不删"，但对 AD 说"内容丢失"是错的 ——
            # 用户会以为远端有一份自己的东西要没了，实际没有。
            # 区分依据：基线里有没有这条记录。
            #   AD = 新建后删除、从未推送 → 基线无记录（b_sha 为空）
            #   MD = 曾记录过、暂存改动后又删 → 基线有记录
            _xy = "AD" if not b_sha else "MD"
            _why = ("有一份未推送的改动会随删除一同消失"
                    if _xy == "MD" else
                    "该文件从未推送过（远端没有它），无需删除")
            return False, (f"本地索引里有该文件的**未推送改动**"
                           f"（git status 为 {_xy}），{_why}，不删")

        # 判据 3：本地必须曾经拥有过。放在这里而不是只在
        # collect_removal_candidates 里，才能覆盖调用方传入的路径。
        if seen_set and path not in seen_set and not force:
            # 不能用基线兜底这一条：基线本身就包含「远端独有、本地从未
            # 有过」的文件（init_baseline 会把它们一并记进基线）。
            # 用 base 兜底等于**取消判据 3** —— 实测会让
            # 「远端独有被拦」这条用例变红，即把别人的文件当成删除。
            return False, "本地从未拥有过此文件（远端独有），不删"
        if not _seen_available and not force:
            # N30：判据 3 不可用（local_seen 缺失）。
            # 此时无法区分「远端独有」与「本地曾有过」—— 放行的代价是
            # 可能删掉别人的文件，所以仍不删；但必须**说清楚**，
            # 否则用户以为没有删除待处理（实测：连 skipped 都是空的）。
            return False, ("local_seen 缺失，无法确认本地曾拥有过此文件，"
                           "不删（重建基线后会自动恢复：--init-baseline）")

        if path in conflicts:
            return False, "有未解决冲突，不删（先 --resolve 再推）"

        # N1：submodule 在 rstate 里取不到（tree API 的 blob 过滤），
        # rsha 恒为 None，下面的「远端被他人改动」判据会短路放行 ——
        # 而那正是这条判据存在的唯一目的。必须显式拦。
        if path in submodules or str(b_mode) == SUBMODULE_MODE:
            return False, ("submodule：远端状态取不到（tree API 按 blob 过滤，"
                           "submodule 的 type 是 commit），无法确认是否被他人改动，不删")

        if b_sha is None:
            # 基线没记录：可能是远端也不存在（本地建了又删），也可能是基线缺项。
            # 不猜，直接跳过 —— 猜错的代价是删掉别人的文件。
            if rsha is None:
                return False, "基线与远端均无此文件，无需删除"
            return False, "基线未记录、远端却存在，无法确认原状态，不删"
        # N32 的判据只在**确实拿到了远端状态**时才成立。
        # rstate 为空 dict 表示「没拿到远端数据」（调用方没传 / 拉取失败），
        # 那是 absence of evidence，不能当成 evidence of absence——
        # 否则所有删除都会被「远端没有」拦掉，删除功能整体失效
        # （实测：普通文件用例传 rstate={} 即被误拦）。
        if rsha is None and rstate:
            # N32：基线有、远端没有 —— 远端早就没了（通常是别人删的，
            # 我还没同步）。原逻辑没有任何分支拦它，直接落到 return True
            # 放行，于是这类「幽灵路径」照样生成删除条目。
            #
            # 实测危害：15 个幽灵 + 6 个真删除，基线 21 → n_pure=21
            # → 超过上限 20 → **整次熔断**。真实待删只有 6 个（远低于
            # 上限），却被 15 个根本不存在的路径凑到熔断。
            # 用户读到的「要删除 21 个」里只有 6 个是真文件，
            # 按提示设 MAX_FILES=26 才能过，实际 11 就够。
            #
            # 三重后果：挤占熔断配额 / 报告数量失真 / 让用户按错的
            # 数字做决策。远端都没有的东西，删它没有意义。
            return False, "远端已无此文件（可能已被他人删除），无需删除"
        if rsha is not None and rsha != b_sha and not force:
            # rsha is None 必须排除：rstate 拿不到该路径时 rsha 为 None，
            # 与「远端内容变了」是两回事。去掉这个保护会让「没拿到远端
            # 状态」被误判成「远端已被他人改动」（实测：普通文件用例
            # 传 rstate={} 即被拦，删除功能整体失效）。
            return (False,
                    f"远端已被他人改动（{str(rsha)[:8]} ≠ 基线 {str(b_sha)[:8]}），不删")
        return True, ""

    def _guard_target(new_path):
        """重命名的新路径校验（M2/M4）。

        旧路径查了远端，新路径却没查 —— 结果是「删掉远端 old.txt，同时
        把本地 new.txt 作为新增推上去」，而远端 new.txt 可能早就被别人
        占用且内容不同。那是一次静默覆盖，正是 _guard 对旧路径严防死守
        的场景，新路径这一侧不能没有同等校验。
        """
        if not new_path:
            return True, ""
        _m, rsha = rstate.get(new_path, (None, None))
        if rsha is None:
            return True, ""
        # N490（A121，低危）：`_m` 取出来后**从未使用** —— 只比 sha 不比
        # mode。于是「重命名目标在远端已存在、内容相同但 mode 不同」
        # （远端 100755、本地 100644）→ 判"相同"→ 放行 → 推上去把远端的
        # 可执行位改掉。
        #
        # 与 N475（第一百五十六轮：基线刷新只比 sha 不比 mode）是同一族的
        # 第 4 个出口：`pull()` 的 cand 判据含 mode、`FileBaselineGuard`
        # 含 mode、`_guard` 对旧路径用 `_entry_mode` 取了 mode，**这里没有**。
        # 而 `rstate` 的条目形态就是 `(mode, sha)`，mode 已经在手上了。
        #
        # 定低危：方向 fail-open，但 mode-only 冲突极少，且删的是旧路径。
        # 修它不为拦下更多，为的是「四个出口口径同源」—— 否则下一个
        # 按 mode 复盘的人会以为这里也含 mode。
        # N635（A120 §4 复核，中危）：这里原先写的是
        #   `_lm = _entry_mode(local_modes.get(new_path))`
        # 而 `_entry_mode(v)` 的定义是
        #   `v.get("mode") if isinstance(v, dict) else None`
        # —— 它要的是**基线条目 dict**，而 `local_modes.get(...)` 给的是
        # **裸 mode 字符串**（"100644"）。非 dict 一律返回 None，
        # 于是 `_lm is not None` 恒假、**整条 mode 判据永不命中**。
        #
        # 也就是说 N490「已修」是死的：代码写了，判据恒不成立，行为与
        # 修之前逐字相同（实测：远端 100755 / 本地 100644 照样放行）。
        #
        # 与 N634 是同一形态（本批反复记的「修法对了但接不上数据源」）：
        # 差别只是 N634 的源集合恒空、这里是类型不匹配。
        # `local_modes` 的值本就是 mode 字符串，直接用即可。
        _lm = local_modes.get(new_path) if local_modes else None
        if (_m is not None and _lm is not None and _m != _lm
                and not force):
            return False, (f"重命名目标 {new_path} 远端已存在且 mode 不同"
                           f"（远端 {_m} ≠ 本地 {_lm}），不删不覆盖")
        if rsha != local_shas.get(new_path) and not force:
            return False, (f"重命名目标 {new_path} 远端已存在且内容不同"
                           f"（{str(rsha)[:8]}），不删不覆盖")
        return True, ""

    for old, new in renames:
        ok, why = _guard(old)
        if not ok:
            skipped.append((old, f"（重命名 {old} → {new}）{why}"))
            continue
        ok2, why2 = _guard_target(new)
        if not ok2:
            skipped.append((old, why2))
            continue
        entries.append(_rm_entry(old, base_files))

    for p in pure:
        ok, why = _guard(p)
        if not ok:
            skipped.append((p, why))
            continue
        entries.append(_rm_entry(p, base_files))

    # ---- 熔断（真删除 + 总数占比两路）----
    # N29：原写法是 `sum(1 for e in entries if e["path"] not in renamed_paths)`，
    # 而 entries 只会 push 通过 _guard 的路径、renames 的旧路径也走同一套
    # 校验，两路并集后 renamed_paths ⊆ entry 路径 —— 这个过滤在跳过场景中
    # 才可能不等。保留但显式注释：它依赖「被跳过的重命名不进 entries」，
    # 这是真的（skipped 分支 continue 掉了），所以过滤不是恒等。
    renamed_paths = {old for old, _n in renames}
    n_pure = sum(1 for e in entries if e["path"] not in renamed_paths)
    n_total = len(entries)
    total_base = max(len(base_files), 1)
    abort, abort_reason = _circuit_break(n_pure, n_total, total_base, limits())

    # abort 时清空 entries：安全不该寄托在调用方记得写 `if abort: return`。
    # 契约写漏一行就是整仓删空，而清空是零成本的。
    #
    # 计数同步归零（L3）：entries 已空，若 pure_delete_count 仍报 25，
    # 调用方无法区分「已经删了 25 个」与「准备删 25 个但被拦」。
    # 原值另存 intended_*，排查熔断原因时还要用。
    # N454：受「查不到」影响的路径**逐条**汇总计数。
    # 上方 `_local_exists` 的提示按 errno 去重（避免刷屏），只点名第一个
    # 文件；若不给总数，读起来就是「只有它一个」。总数才是告知的完整形态。
    if _LOCAL_EXISTS_UNKNOWN:
        _ui = get_ui()
        _line = (f"  ! 共 {len(_LOCAL_EXISTS_UNKNOWN)} 个路径无法判断"
                 f"是否存在（按「还在」处理，本次都不删）")
        if _ui is not None:
            _ui.line(_line)
        else:
            print(_line)

    intended_pure, intended_total = n_pure, n_total
    if abort:
        entries = []
        n_pure = n_total = 0

    return {
        "entries": entries,
        "renames": renames,
        "skipped": skipped,
        # N489 / N491：被 .gitignore 屏蔽（或不可编码）的「新去处」。
        # 空 = 没有这类情形；非空时调用方**必须点名**——删除关闭时尤其
        # 重要（N491）：那种情况下用户唯一能做的就是取消屏蔽，而输出
        # 原先不给这个方向。
        "shadowed": shadowed,
        "abort": abort,
        "abort_reason": abort_reason,
        "derived_deletes": derived,
        "pure_delete_count": n_pure,
        "total_delete_count": n_total,
        "intended_pure_count": intended_pure,
        "intended_total_count": intended_total,
    }


def _rm_entry(path, base_files):
    """删除条目。mode 与 type 都沿用基线原值。

    mode 沿用：原文件是 100755（可执行脚本）时写 100644 虽不影响删除本身
    （sha: null 才是关键），但会让 tree 条目与实际历史不一致，
    排查「为什么这个文件变了」时多一个误导点。

    type 同样要跟着走（N4）：submodule 的 type 是 commit，不是 blob。
    只尊重一半（mode 沿用、type 硬编码）比都不尊重更让人困惑 ——
    读者会以为整条条目都对得上历史。
    """
    mode = str(_entry_mode(base_files.get(path)) or "100644")
    return {"path": path, "mode": mode,
            "type": TYPE_BY_MODE.get(mode, "blob"), "sha": None}


def _safe_rel(p):
    """路径是否在仓库内（相对、无 .. 逃逸、非绝对）—— **弱**判据。

    N633（A128，低危）：与 `safe_rel()` / `_inside_root()`（强判据，走
    realpath）是**两套口径**，不是同一判据的两份实现：弱版不解析符号链接，
    所以会放过 `etcdir/passwd`（`etcdir -> /etc`）；同时它对
    `sub/../a.txt` 返回 False，而强版返回 `a.txt` —— 即它**也会误杀合法
    路径**。两边都不等价，不能互相替换。

    命名带 `_` 前缀原本是想表达"内部"，但恰好与强判据只差一个下划线，
    看名字分不出强弱。这里是**弱**的：只挡显式越界（绝对路径与 `..`）。

    本模块的输入来自基线 / git status，都是 git 认为的仓库内路径，
    所以只需挡住显式越界，无需 realpath 级别校验 —— 这是**有意的取舍**，
    不是漏。若要挡符号链接逃逸，请用 `safe_rel()`。
    """
    if not p:
        return False
    if p.startswith("/") or (os.path.isabs(p) if "nt" not in os.name else False):
        return False
    if p.startswith("~"):
        return False
    parts = p.replace("\\", "/").split("/")
    if ".." in parts:
        return False
    return True


def _default_gitignored(rel):
    """保守默认。真实用法应传 push_api 的 gitignored_set()。

    只挡这两类：目录项（git 不会跟踪裸目录）与 .git/ 内部。
    build/ node_modules/ 这类**只能**靠真正的 check-ignore 判断 ——
    它们是否被忽略完全取决于仓库的 .gitignore，猜不出来。
    """
    return rel.endswith("/") or rel.startswith(".git/")


# ---------------------------------------------------------------- 输出

def _disp_path(p):
    """把路径变成**一定能 print 出去**的形态。

    N22：含 surrogate 的路径（非 UTF-8 文件名经 surrogateescape 解码）
    打印时会按 stdout 的 error handler 分叉：surrogateescape 下输出乱码，
    `utf-8:strict`（常规终端 / CI 常见）下直接抛 UnicodeEncodeError。
    两种都不是正确行为 —— 乱码看不出是哪个文件，崩溃把"文件名有问题"
    报成了一个编码栈。这里统一反斜杠转义，路径仍然认得出。

    N600（A155，低）：转义后显示的是 `'bad\\udcff.txt'` 这种形态，与
    磁盘上的真实文件名**不是同一个字符串** —— 用户照它去跑 `git rm`
    或 `--force-file` 会拿到「路径不存在」。所以调用点必须说明「此处
    为转义显示」，不能只打印转义后的串。
    """
    try:
        p.encode("utf-8")
        return p
    except (UnicodeEncodeError, AttributeError):
        try:
            return p.encode("utf-8", "backslashreplace").decode(
                "utf-8", "replace")
        except Exception:
            return repr(p)


def report_removals(rm, dry_run=False, skipped_non_utf8=0, ui=None):
    """把删除决策说清楚。删除不可逆，输出宁可啰嗦。

    ui  push_api 的 Ui 实例（可选）。传了就走统一出口，`--json` 模式才能
        收集到这些行 —— 否则结构化输出覆盖不到删除路径（L6）。
        不传时降级为直接 print，text 模式下输出完全一致。

    skipped_non_utf8  本次运行被「非 UTF-8」挡在 local_seen 之外的
        路径数（M44/A54）。此前**从未有调用点传入**，恒为默认值 0，
        于是下面那句提示永不打印 —— 而 sync_local_seen 明明返回了这个
        数（N7 就要求了），只是 6/7 个调用点丢弃返回值。
        现在由 _NON_UTF8_DROPPED（per-run 集合）接线进来。
    """
    def _say(text, kind="info"):
        if ui is not None and hasattr(ui, "line"):
            ui.line(text, kind)
        else:
            print(text)

    renames, entries, skipped = rm["renames"], rm["entries"], rm["skipped"]
    aborted = bool(rm.get("abort"))
    tag = "将删除" if not dry_run else "将删除（dry-run）"

    # N20：熔断 abort 时 entries 已被清空，但 renames **没有**。
    # 于是「已中止」的报告里照样列着「远端旧路径将删除」——
    # 用户看到「将删除」+ 一长串名单，很可能以为已经删了。
    # abort 意味着一个都没删，renames 必须改口径。
    if aborted:
        tag = "已中止，未删除"

    # 被 skip 的旧路径：报告里不能再写「将删除」（N3）。
    skipped_paths = {p for p, _why in skipped}

    # N24：截断策略要一致。原先重命名**全量**打印、删除条目截断到 20，
    # 于是一次 200 个文件的改名会刷满整个终端，而真正危险的删除清单
    # 反而只显示 20 条 —— 信息密度与安全权重正好反了。
    # 统一用同一个上限，且删除优先（它才是不可逆且需要人工核对的）。
    CAP = 20

    # N22：路径显示前一律消毒。plan_removals 已把不可编码的 `added`
    # 挡在配对之外，但**显示侧不该依赖上游永远干净** —— 任何一条
    # 绕过那条过滤的路径都会让整份报告崩在 print 上（栈停在下面这几行），
    # 而崩溃点是"报告阶段"，用户看到的是 UnicodeEncodeError 而不是
    # "有个文件名有问题"。与起飞前消毒同形态：出口处再挡一次。
    for old, new in renames[:CAP]:
        _o, _n = _disp_path(old), _disp_path(new)
        # N600（A155，低）：消毒过的串与磁盘上的真实文件名**不是同一个
        # 字符串**（surrogate 显示为 `\udcff`）。不说明的话，用户照它去
        # 跑 `git rm` / `--force-file` 会拿到「路径不存在」，而报告里
        # 没有任何一处提过"这是转义显示"。只在**真的发生了转义**时才说，
        # 避免给正常路径平添噪音。
        if _o != old or _n != new:
            _say("  （下面的路径含非 UTF-8 字节，此处为转义显示，"
                 "照抄命令会找不到文件）", "warn")
        if old in skipped_paths:
            why = next((w for p, w in skipped if p == old), "")
            _say(f"  ≈ {_o} → {_n}（重命名，已跳过：{why}）", "warn")
        elif aborted:
            # N20：abort = 一个都没删，不能再写「将删除」
            _say(f"  ≈ {_o} → {_n}（重命名，本次已中止，未删除）", "warn")
        else:
            _say(f"  ≈ {_o} → {_n}（重命名，远端旧路径将删除）")
    if len(renames) > CAP:
        _say(f"      ... 重命名还有 {len(renames) - CAP} 个")

    if entries:
        paths = [e["path"] for e in entries]
        # N26：entries 里**包含**重命名的旧路径，而上面 renames 段已列过
        # 一遍。两份相加会让「本次要删多少个」这个数字虚高一倍 ——
        # 用户拿它跟自己删的文件数对不上，就会怀疑工具出错。
        # 这里显式拆开说，数字才对得上。
        n_ren = len([p for p in paths if p in {o for o, _n in renames}])
        n_pl = len(paths) - n_ren
        if n_ren:
            _say(f"\n  ⚠️  {tag} {len(paths)} 个远端路径（不可逆）"
                 f"：其中重命名 {n_ren} 个、真删除 {n_pl} 个：", "warn")
        else:
            _say(f"\n  ⚠️  {tag} {len(paths)} 个远端文件（不可逆）：", "warn")
        for p in paths[:CAP]:
            # N599（A155，中）：`renames` 段（上面 46 行处）走了
            # `_disp_path`，而本段（entries）与下面的 skipped 段是**裸
            # 路径** —— 三个打印点里只补了一个。
            #
            # 在 `utf-8:strict` 的 stdout 下直接 `UnicodeEncodeError`
            # 崩溃（实测复现，栈停在 report_removals）；在
            # surrogateescape 下输出乱码。而这两段装的恰恰是**最需要
            # 被读到的**信息：即将不可逆删除的路径、以及被跳过的路径。
            _say(f"      - {_disp_path(p)}", "warn")
        if len(paths) > CAP:
            _say(f"      ... 还有 {len(paths) - CAP} 个", "warn")

    # N27：abort 时「未删除」这个措辞暗示「其他的都删了」，
    # 实际是一个都没删。措辞必须区分这两种情形。
    # N28：skipped 原先完全不截断，与 entries 的策略不一致 ——
    # 一次 200 个文件的改名会刷出 200 行 skip 明细。
    # N27：abort 时「未删除」暗示「其他的都删了」，实际一个都没删。
    # 措辞必须区分这两种情形。
    skip_tag = "未删除" if not aborted else "未删除（本次全部中止，一个都没删）"
    for p, why in skipped[:CAP]:
        _say(f"  · {_disp_path(p)} {skip_tag}：{why}")
    if len(skipped) > CAP:
        _say(f"      ... 未删除的还有 {len(skipped) - CAP} 个")

    # N35：derived_deletes 此前**无人消费**（三个文件里 0 引用、0 断言）。
    # 它标记「调用方没传 deleted、模块走了降级路径」—— 第五轮 N16
    # 发现的正属于这一类（门控只用 git status，已 commit 的删除到不了）。
    # 如果当初把它显示出来或断言它必须为 False，那类降级会立刻暴露，
    # 而不是等用户发现「文件删不掉」才被追查。
    if rm.get("derived_deletes"):
        _say("  ! 本次删除清单由模块自行推导（调用方未传 git status 信号）"
             "—— 已 commit 的删除可能漏掉", "warn")

    # 静默过滤必须说出口（N7）：这些文件永久无法被删除检测覆盖，
    # 用户不知情就会一直困惑「为什么我删了它却没反应」。
    if skipped_non_utf8:
        _say(f"  · {skipped_non_utf8} 个非 UTF-8 路径已排除在删除检测外"
             f"（请改名为 UTF-8 文件名后再删）", "warn")

    if rm["abort"]:
        _say(f"\n❌ {rm['abort_reason']}", "error")


# -------- 删除与重命名：绑定到本文件内的实现 --------
# 放在这里（而不是文件开头）是因为上面的函数定义必须已经执行完。
#
# 安全模型：合并后代码总在内存里，无法再靠「不加载」来防御，
# 只能「不启用」。两个开关各管一件事：
#   PUSH_API_ENABLE_DELETE —— 删除能力的独立开关（默认关）
#
# N342（第一百〇六轮，低危）：这里原本还写着
#   "PUSH_API_PLUGINS —— 兼容开关……两者都要开才生效"
# 而 PUSH_API_PLUGINS 全文件**只在这一行注释里出现**、零代码读取；
# 同时 _PLUGIN_DIR / _PLUGIN_DIR_ABS / _PLUGIN_LOADED_FROM 三个模块级
# 变量经 AST 扫描确认：赋值 1 次、读取 **0 次**。
#
# 好消息是用户不会被误导：_delete_how_to_enable() 只给 --delete 和
# PUSH_API_ENABLE_DELETE，没提 PLUGINS。受影响的是**照注释去改代码的
# 维护者** —— 会去找一个不存在的开关，或以为还要额外开一个。
# 删除是否启用的**唯一判据**是 _deletes_disabled()（见其定义）。
# 这里刻意不再引入第二套判断 —— 本轮修的几十条问题里，有相当一部分
# 正是「同一件事两处判据、改一漏一」。合并进单文件后更容易顺手再加一个，
# 那恰恰是合并要消除的东西。

def _bind_removals():
    """按当前开关把删除实现绑定（或解绑）到全局名字。

    **必须可重入**：末尾的模块级绑定只看环境变量，而 `--delete` 是
    单次开关，要等 parse_args 之后才知道。若绑定只在模块级做一次，
    `python3 push_api.py --delete` 会拿到 _plan_removals = None，
    即命令行开关**完全失效**（只有环境变量能开）——
    而提示里明确写着"本次开启：--delete"，说了却做不到。

    判据用唯一的 _deletes_disabled()，不再叠加别的开关
    （合并进单文件后已无"外部插件加载"，再要求第二个开关
    只会是又一处"同一件事两套判据"）。
    """
    global _plan_removals, _report_removals, _sync_local_seen, _PLUGIN_ERROR
    if _deletes_disabled():
        _plan_removals = _report_removals = None
        # N101：**不**把 _sync_local_seen 一起关掉。
        # 它是"记录本地曾经拥有过哪些文件"的清单，与"是否执行删除"
        # 是两件事。被连带关闭的后果：关闭期间新增的文件永远进不了清单，
        # 日后用户启用删除时，这些文件会被判成"远端独有"而漏删 ——
        # 而它们恰恰是本地真实拥有过的。
        # 更糟的是这个缺口**不可回填**：需要清单的时刻（文件已删）
        # 与能补清单的时刻（文件还在）互斥。
        _sync_local_seen = sync_local_seen
        _PLUGIN_ERROR = "默认关闭，删除不可逆"
    else:
        _plan_removals = plan_removals
        _report_removals = report_removals
        _sync_local_seen = sync_local_seen
        _PLUGIN_ERROR = None


_bind_removals()          # 模块级先绑一次（环境变量 / 默认）


if __name__ == "__main__":
    try:
        main()
        # 被防护拦下不是错误：改动没丢，按提示处理即可。
        # 退出码 2 让 CI 能把它与「真出错」分开（见 GuardBlock 的说明）。
        #
        # N240（**已修**，本注释原先误标为"未修"—— N291）：
        # `main()` 的 finally 里调 reset_per_run_globals()，
        # 它**必然先于**这里执行，会把 _BLOCKED_BY_GUARD 清成 None ——
        # 于是判据恒 False，退出码恒 0。
        # 修法：复位前先把值转移到 _LAST_RUN_BLOCKED，顶层读它。
        #
        # M42：注意 —— 它确实在复位清单里（跨运行不串味所必需），
        # 所以真正的修法是"先取值 → 再复位 → 最后写回"（见 main 的
        # finally），而不是把它挪出清单。挪出去会让上一次被拦下的
        # 结果污染下一次成功运行，退出码假报 2。
        #
        # 受影响的是两道最常见的 return 型 Guard：
        # RewindGuard（本地回退）与 StaleLocalGuard（本地落后）。
        # 契约是 2，实际是 0 —— CI 会把「被拦下」判成推送成功。
        #
        # N362：取消排在拦下**之后**判 —— 两者互斥（取消时不会有 Guard
        # 置位），顺序只为可读性。（这里已在模块级作用域，无需 global。）
        if _LAST_RUN_BLOCKED:
            sys.exit(2)
        if _LAST_RUN_CANCELLED:
            # 4 = 取消：这次没跑成，需要重跑（非交互场景多半要加 --yes）。
            # 不能是 0（CI 会当成功），也不能是 1（那是"出错"，
            # 而取消是正常流程）。
            sys.exit(4)
    except GuardBlock as e:
        # 被防护拦下：改动没丢，只是需要人按提示处理。
        # 走 2 而不是 1，让 CI 能把「被拦下」与「真出错」分开 ——
        # 否则要么全部告警（噪音，久了没人看），要么全部忽略
        # （真丢改动时同样没人看）。
        if str(e):
            print(str(e))
        sys.exit(getattr(e, "code", 2))
    except KeyboardInterrupt:
        # 中断是正常操作，不该甩一堆堆栈。
        # 但要提醒一句：远端可能已有本次推送的内容 —— 分支由
        # _cleanup_failed_branch 负责清理，本地改动一直在工作区。
        #
        # M126（A116）：N480 装了 SIGTERM / SIGHUP 的 handler 之后，
        # 它们也走这一支。若仍无差别地说「Ctrl-C」，CI 超时被杀、
        # 终端断开（SIGHUP）的用户会去查一个根本没发生过的按键 ——
        # 归因错了，后面的排查全部白费。
        #
        # 退出码同理：130 是 Ctrl-C 的惯例值，而 POSIX 对"被信号终止"
        # 的约定是 128 + sig（SIGTERM=15 → 143，SIGHUP=1 → 129）。
        # CI 靠退出码区分"用户主动中断"与"被超时策略杀掉"，
        # 一律 130 会把后者混进前者。
        _sig = _LAST_TERM_SIGNAL
        if _sig:
            try:
                _nm = _signal.Signals(_sig).name if _signal else str(_sig)
            except Exception:                        # noqa: BLE001
                _nm = str(_sig)
            print(f"\n\n已终止（收到 {_nm}，非 Ctrl-C）。本地改动未丢失；"
                  f"若中断发生在推送之后，跑 --prune 可查看残留分支。\n"
                  f"   CI 场景：这通常是超时策略或会话结束发出的信号，"
                  f"不是用户按键 —— 排查方向请按信号来。")
            sys.exit(128 + int(_sig))
        print("\n\n已中断（Ctrl-C）。本地改动未丢失；"
              "若中断发生在推送之后，跑 --prune 可查看残留分支。")
        sys.exit(130)
    except EOFError:
        # 非交互环境（CI / 管道）里 input() 直接抛 EOFError。
        # 没有这个兜底，用户看到的是裸 traceback 而不是「请加 --yes」。
        print("\n输入已关闭（非交互环境），无法等待确认。"
              "请加 --yes 或 -y 跳过交互。")
        sys.exit(1)
