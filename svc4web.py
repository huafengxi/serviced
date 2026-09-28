#!/usr/bin/env python3
# -*- type=script -*-
# svc 服务状态报表（全机器聚合）· 8080 实时端点。
#   初版：仅本机 `svc.py status` 纯文本。
#   本版：聚合 dev/mac/nv1/nv2 四机，输出 markdown
#   （dash.itab 消费：/svc/svc4web.py?v=text/md&refresh=30）。
#
# 机器面：本机（env/host-id 规范名命中主机清单者）直接 subprocess 跑
# `python3 svc/svc.py status --machine`；其余各机经反向命令通道
# `rsh/rsh <host> -t N -- 'cd ~/m && python3 svc/svc.py status
# --machine'`（远端只读，仅 status）。全部并行（ThreadPoolExecutor），每机
# 超时上限 ≤20s（rsh 侧 18s 先杀，本地兜底 20s），总耗时不拖垮 30s 刷新的
# tab。离线/超时/非零退出的机器渲染降级区块，不影响其他机器。
#
# 退出码语义（rsh/README.md）：124 = 远端超时；125 = 本地错误（rshd 没起、
# worker 不在线等）。rsh 在线状态独立成段：CLI `svc4web.py rsh-online` 单次
# `rsh -l` 渲染 rsh 通道 worker 表；per-host widget 与
# interp 均不再调 `rsh -l`，节头改中性标注（本机/远端）。
#
# 展示规则（2026-08-31 用户拍板；同日用户补充拍板）：
# 行展示条件 = actual==online OR (desired==online 且 state 不以 'skip' 开头)。
# 实际在线的服务一律展示（含本机应 skip 但确实在线的，STATE 列自带 skip 字样
# 一眼可辨）；预期在线的也展示，但应 skip 的服务（本机不管理）若实际 offline
# 则隐藏——skip 行只在「实际 online」时展示（补充口径；现例：本机
# agents-sync desired=online/actual=offline/state='skip (…)')。旧规则
# （「隐藏所有 skip 行」）已废止；
# show_skip=1 开关语义随之改为「展示全部行（含双 offline 的）」，参数名保
# 留兼容旧链接。数据层配套改动：svc.py 对 skip 服务也做真实探活（pidfile），
# 但 skip 行永不计 drift；sync 对 skip 服务的处置也未变（skip = 本机不管理 →
# 永不启停/重启，drift 与 stale 恒 False）。
#
# section 标题归 markdown 文档：输出不再带 `## `
# 标题行——标题由消费方文档（svc.md 静态二级标题）提供，输出只给数据；
# 原节头动态摘要（本机/远端、drift、服务计数、隐藏数）降级为输出的首行
# 普通行，信息逐字等价。
#
# 测试钩（生产不设）：
#   SVC_HOSTS           逗号分隔主机清单覆盖（注入假主机名试降级路径）
#   SVC_STATUS_PATH     覆盖本机 svc.py 路径
#   SVC_STATUS_TIMEOUT  每机超时（秒，仅可向下调，上限锁死 20s）
import json
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

_HOSTS_DEFAULT = ['dev', 'mac', 'nv1', 'nv2']
_PER_HOST_CAP = 20.0   # 每机本地兜底超时上限（秒），锁死；环境钩仅可向下调
_RSH_TIMEOUT = 18.0    # rsh -t：远端先杀，留 2s 余量给本地兜底
_RSH_LIST_TIMEOUT = 5.0
_STDERR_LIMIT = 1000


def _self_dir():
    # type=script 经 exec 执行（无 __file__）；server 进程 chdir 到 web 根
    # （~/m），故回退 = web 根下的 svc/ 目录
    try:
        return os.path.dirname(os.path.realpath(__file__))
    except NameError:
        return os.path.realpath('svc')


def _local_host_id(ws):
    """本机规范名：env/host-id 按 $(hostname) 查表，未命中回退 hostname。"""
    import platform
    hn = platform.node().lower()
    try:
        with open(os.path.join(ws, 'env', 'host-id')) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == hn:
                    return parts[1]
    except OSError:
        pass
    return hn


def _rsh_list(rsh):
    """`rsh -l` 解析：返回行列表（每行 [worker, connected, last-seen, fwd]）；
    任何失败（超时/非零退出/无输出）返回 None。供 rsh-online renderer 用。"""
    try:
        r = subprocess.run([rsh, '-l'], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=_RSH_LIST_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.decode('utf-8', 'replace').splitlines()[1:]:
        parts = line.split()
        if parts:
            rows.append((parts + ['-', '-', '-'])[:4])
    return rows


def _render_rsh_online(rows):
    """rsh 通道 markdown 表（标题由消费文档提供，见模块头注释）；
    rows=None 时渲染降级区块（仍 exit 0）。"""
    if rows is None:
        return ('**降级 · rsh -l 失败或超时**\n'
                'worker 在线状态不可知，见下方各 host widget。\n')
    lines = ['| worker | connected | last-seen | fwd |',
             '| --- | --- | --- | --- |']
    for w, c, ls, fwd in rows:
        lines.append('| %s | %s | %s | %s |' % (
            _md_cell(w), _md_cell(c), _md_cell(ls), _md_cell(fwd)))
    return '\n'.join(lines) + '\n'


def _clip(text):
    if len(text) > _STDERR_LIMIT:
        return text[:_STDERR_LIMIT] + '\n…(截断)'
    return text


def _parse_machine_output(stdout):
    """svc.py status --machine 单行 JSON → dict；解析失败返回 None。"""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith('{'):
            try:
                data = json.loads(line)
            except ValueError:
                return None
            if isinstance(data, dict) and isinstance(data.get('services'), list):
                return data
    return None


def _fetch_local(host, svc_path, timeout):
    cmd = ['python3', svc_path, 'status', '--machine']
    res = {'host': host, 'local': True}
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        res.update(ok=False, title='执行超时（每机上限 %gs）' % timeout,
                   detail='cmd: %s' % ' '.join(cmd))
        return res
    except OSError as e:
        res.update(ok=False, title='启动失败', detail=repr(e))
        return res
    if r.returncode != 0:
        res.update(ok=False, title='非零退出: exitcode=%d' % r.returncode,
                   detail=_clip(r.stderr.decode('utf-8', 'replace')) or '(无 stderr)')
        return res
    data = _parse_machine_output(r.stdout.decode('utf-8', 'replace'))
    if data is None:
        res.update(ok=False, title='机器可读输出解析失败',
                   detail=_clip(r.stdout.decode('utf-8', 'replace')))
        return res
    res.update(ok=True, data=data)
    return res


def _fetch_remote(host, rsh, timeout):
    # 远端命令固定为只读 status 调用（任务约束）；经 rsh 反向通道下发。
    # 注意：worker 侧对每个 argv 元素 shlex.quote 后经 bash -c 执行，
    # 故含 && 的 shell 串必须整体作为 `bash -lc` 的单个参数传入。
    # python 回退：mac 的默认 python3（homebrew）无 yaml，仅 ~/miniconda3
    # 的 python 可跑 svc.py——首选命令不变，失败时回退 conda python
    # （status --machine 成功恒返 0，回退只在首选失败时触发）。
    remote_cmd = ('cd ~/m && python3 svc/svc.py status --machine'
                  ' || ~/miniconda3/bin/python3 svc/svc.py status --machine')
    cmd = [rsh, host, '-t', str(_RSH_TIMEOUT), '--',
           'bash', '-lc', remote_cmd]
    res = {'host': host, 'local': False}
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        res.update(ok=False, title='执行超时（每机上限 %gs）' % timeout,
                   detail='cmd: %s' % ' '.join(cmd))
        return res
    except OSError as e:
        res.update(ok=False, title='启动失败', detail=repr(e))
        return res
    if r.returncode != 0:
        stderr = _clip(r.stderr.decode('utf-8', 'replace'))
        if r.returncode == 124:
            title = '远端超时（rsh exit 124）'
        elif r.returncode == 125:
            title = 'rsh 本地错误 / worker 不在线（rsh exit 125）'
        else:
            title = '远端非零退出: exitcode=%d' % r.returncode
        res.update(ok=False, title=title, detail=stderr or '(无 stderr)')
        return res
    data = _parse_machine_output(r.stdout.decode('utf-8', 'replace'))
    if data is None:
        res.update(ok=False, title='机器可读输出解析失败',
                   detail=_clip(r.stdout.decode('utf-8', 'replace')))
        return res
    res.update(ok=True, data=data)
    return res


def _md_cell(s):
    return str(s).replace('|', '\\|')


def _render(res, show_all=False):
    """单机区块（markdown）：首行摘要 + 表格，或降级说明。

    显示层过滤（2026-08-31 用户拍板；同日补充拍板）：
    行展示条件 = actual==online OR (desired==online 且 state 不以 'skip'
    开头) —— 实际在线一律展示（含 skip 但在线者）；预期在线的也展示，但应
    skip 的服务（本机不管理）实际 offline 时隐藏，即 skip 行只在「实际
    online」时展示。其余隐藏行在摘要中以「隐藏 N」标注。show_all=True（请求带
    show_skip=1，参数名兼容旧语义、现为「展示全部行」）时展示所有行。
    svc.py 口径不变。
    标题归文档：不输出 `## ` 节头，动态摘要以首行普通行
    保留（信息逐字等价）。
    """
    if res.get('ok'):
        data = res['data']
        # 摘要中性标注：不依赖 rsh -l；在线状态见 rsh 通道段
        n_online = '本机' if res.get('local') else '远端'
        services = data['services']
        total = data.get('total', len(services))
        shown = services if show_all else [
            s for s in services
            if str(s.get('actual', '')) == 'online'
            or (str(s.get('desired', '')) == 'online'
                and not str(s.get('state', '')).startswith('skip'))]
        hidden = len(services) - len(shown)
        head = '%s · drift %s · %d/%d services' % (
            n_online, data.get('drift', '?'), len(shown), total)
        if hidden:
            head += ' · 隐藏 %d（双 offline，或 skip 且实际 offline）' % hidden
        head += '\n\n'
        lines = ['| SERVICE | VERSION | DESIRED | ACTUAL | STATE |',
                 '| --- | --- | --- | --- | --- |']
        for s in shown:
            lines.append('| %s | %s | %s | %s | %s |' % (
                _md_cell(s.get('name', '')), _md_cell(s.get('version', '-')),
                _md_cell(s.get('desired', '')), _md_cell(s.get('actual', '-')),
                _md_cell(s.get('state', ''))))
        return head + '\n'.join(lines) + '\n'
    body = '降级（offline）\n\n**%s**\n' % res.get('title', '未知错误')
    detail = res.get('detail') or ''
    if detail:
        body += '\n```\n' + '\n'.join(detail.splitlines()) + '\n```\n'
    return body


def interp(store, **kw):
    ws = os.path.dirname(_self_dir())  # ~/m
    rsh = os.path.join(ws, 'rsh', 'rsh')
    svc_path = os.getenv('SVC_STATUS_PATH') or os.path.join(_self_dir(), 'svc.py')
    timeout = min(_PER_HOST_CAP, float(os.getenv('SVC_STATUS_TIMEOUT')
                                       or _PER_HOST_CAP))
    hosts_env = os.getenv('SVC_HOSTS')
    hosts = ([h.strip() for h in hosts_env.split(',') if h.strip()]
             if hosts_env else list(_HOSTS_DEFAULT))
    local = _local_host_id(ws)
    # 展示开关：请求带 show_skip=1 时展示全部行（含双 offline 的）；
    # 参数名沿用旧开关兼容链接，语义已改（2026-08-31 用户拍板）
    show_all = str(kw.get('show_skip', '')) == '1'

    # 并行抓取：每机一个线程，各自带 subprocess 超时；总等待 = 每机上限 + 2s
    with ThreadPoolExecutor(max_workers=max(len(hosts), 1)) as pool:
        futures = {}
        for h in hosts:
            if h == local:
                futures[h] = pool.submit(_fetch_local, h, svc_path, timeout)
            else:
                futures[h] = pool.submit(_fetch_remote, h, rsh, timeout)
        results = []
        for h in hosts:
            try:
                results.append(futures[h].result(timeout=timeout + 2))
            except Exception as e:  # TimeoutExpired / 其他兜底：降级不拖垮整页
                results.append({'host': h, 'local': h == local, 'ok': False,
                                'title': '抓取失败: %s' % e.__class__.__name__,
                                'detail': str(e)})

    ok_hosts = [r for r in results if r.get('ok')]
    n_down = len(results) - len(ok_hosts)
    total_drift = sum(r['data'].get('drift', 0) for r in ok_hosts)
    down_names = ', '.join(r['host'] for r in results if not r.get('ok'))

    parts = ['# svc 服务状态报表 · 全机器\n',
             '**汇总**：%d 机 · 在线 %d / 离线 %d · 总 drift %d' % (
                 len(results), len(ok_hosts), n_down, total_drift)]
    if n_down:
        parts[-1] += '（离线/降级：%s）' % down_names
    parts.append('')
    for r in results:
        parts.append(_render(r, show_all))
    body = '\n'.join(parts)
    logging.info('svc4web: %d hosts (%d down), total drift %d, %d bytes',
                 len(results), n_down, total_drift, len(body))
    return dict(type='text/markdown'), body


def _main(argv):
    """CLI 入口：
    `python3 svc4web.py rsh-online`        单次 rsh -l → rsh 通道 worker 表
    `python3 svc4web.py <host> [show_skip=1]` 单 host 区块（不再调 rsh -l）

    输出 markdown 区块（与 interp 中 _render 同源），供每 host 一个
    markdown cmd widget 的 svc.md 消费；标题由 svc.md 静态二级标题提供，
    输出不含 `## ` 行，动态摘要为首行普通行。抓取失败仍输出降级区块并 exit 0
    （stderr 保持干净）。
    """
    if len(argv) < 2:
        sys.stderr.write(
            'usage: python3 svc4web.py rsh-online | <host> [show_skip=1]\n')
        return 2
    ws = os.path.dirname(_self_dir())
    rsh = os.path.join(ws, 'rsh', 'rsh')
    if argv[1] == 'rsh-online':
        sys.stdout.write(_render_rsh_online(_rsh_list(rsh)))
        return 0
    host = argv[1]
    show_all = len(argv) > 2 and argv[2] == 'show_skip=1'
    svc_path = os.getenv('SVC_STATUS_PATH') or os.path.join(_self_dir(), 'svc.py')
    timeout = min(_PER_HOST_CAP, float(os.getenv('SVC_STATUS_TIMEOUT')
                                       or _PER_HOST_CAP))
    local = _local_host_id(ws)
    try:
        if host == local:
            res = _fetch_local(host, svc_path, timeout)
        else:
            res = _fetch_remote(host, rsh, timeout)
    except Exception as e:
        res = {'host': host, 'local': host == local, 'ok': False,
               'title': '抓取失败: %s' % e.__class__.__name__,
               'detail': str(e)}
    sys.stdout.write(_render(res, show_all))
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv))
