import csv
import io
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite


TIME_FORMAT = '%Y-%m-%d %H:%M'
PENDING = '待完工'
DONE = '已完工'
DEFAULT_SHIFTS = '90,120,150'


@dataclass(frozen=True)
class Order:
    code: str
    reach: str
    start: datetime
    snapshot: datetime
    load: float
    band: str
    actual: float
    recorded: datetime | None

    @property
    def day(self):
        return self.start.date().toordinal()


def sample_rows() -> list[dict]:
    first = datetime(2026, 11, 10, 8)
    patterns = [
        ('柳湾上段', 1.4, 54), ('柳湾上段', 4.6, 106),
        ('青浦桥段', 2.2, 65), ('青浦桥段', 8.5, 155),
    ]
    rows = []
    for offset in range(14):
        day = first + timedelta(days=offset)
        snapshot = (day - timedelta(days=1)).replace(hour=17, minute=30)
        for slot, (reach, load, typical) in enumerate(patterns):
            start = day + timedelta(minutes=30 * slot)
            actual = typical + ((offset * 7 + slot * 3) % 15 - 7) * 2
            recorded = (day + timedelta(days=(offset == 8 and slot == 3))).replace(hour=14)
            rows.append({
                'order_id': f'QP-{day:%m%d}-{slot + 1:02}', 'reach': reach,
                'planned_start': start.strftime(TIME_FORMAT), 'snapshot_at': snapshot.strftime(TIME_FORMAT),
                'debris_m3': load, 'status': DONE, 'duration_min': actual,
                'recorded_at': recorded.strftime(TIME_FORMAT),
            })
    for slot, (reach, load, start) in enumerate([
        ('柳湾上段', 1.8, '08:00'), ('柳湾上段', 4.9, '08:45'), ('青浦桥段', 8.1, '09:30'),
    ], start=1):
        rows.append({
            'order_id': f'QP-1124-{slot:02}', 'reach': reach,
            'planned_start': f'2026-11-24 {start}', 'snapshot_at': '2026-11-23 17:30',
            'debris_m3': load, 'status': PENDING, 'duration_min': 0, 'recorded_at': PENDING,
        })
    return rows


def _time(value, label):
    try:
        parsed = datetime.strptime(value, TIME_FORMAT)
    except (ValueError, TypeError) as error:
        raise ValueError(f'{label}请填写北京时间 YYYY-MM-DD HH:MM。') from error
    if parsed.isoformat(' ', timespec='minutes') != value or parsed.year < 1900:
        raise ValueError(f'{label}请填写 1900 年起的北京时间 YYYY-MM-DD HH:MM。')
    return parsed


def _number(value, low, high, label):
    if type(value) not in (int, float) or not low <= value <= high or not isfinite(value):
        raise ValueError(f'{label}请填写 {low} 至 {high} 之间的有限数值并核对单位。')
    return value


def _orders(rows):
    seen = set()
    orders = []
    for line, row in enumerate(rows, 2):
        try:
            code, reach = row['order_id'], row['reach']
            if not isinstance(code, str) or not code.strip() or not isinstance(reach, str) or not reach.strip():
                raise ValueError('请补填工单号与河段名称。')
            code, reach = code.strip(), reach.strip()
            if code in seen:
                raise ValueError(f'工单 {code} 重复，请保留一份用于预报的冻结快照。')
            start = _time(row['planned_start'], '计划开工')
            snapshot = _time(row['snapshot_at'], '巡查快照')
            if snapshot > start:
                raise ValueError('巡查快照晚于计划开工，请换成开工前已留存的快照。')
            load = _number(row['debris_m3'], 0, 1000, '预估漂浮物量（m³）')
            band = '低量' if load < 3 else '中量' if load < 7 else '高量'
            if row['status'] == DONE:
                actual = _number(row['duration_min'], 1, 1440, '实耗（分钟）')
                recorded = _time(row['recorded_at'], '完工台账入账')
                if recorded < start:
                    raise ValueError('完工台账入账早于计划开工，请核对开工与入账时间。')
            elif row['status'] == PENDING:
                actual = _number(row['duration_min'], 0, 0, '待完工工单实耗（分钟）')
                if row['recorded_at'] != PENDING:
                    raise ValueError('待完工工单的入账时间请填写“待完工”。')
                recorded = None
            else:
                raise ValueError('工单状态请选择“已完工”或“待完工”。')
            orders.append(Order(code, reach, start, snapshot, load, band, actual, recorded))
            seen.add(code)
        except KeyError as error:
            raise ValueError(f'第 {line} 行缺少 {error.args[0]}，请按演示 CSV 补齐字段。') from error
        except ValueError as error:
            raise ValueError(f'第 {line} 行 {error}') from error
    return sorted(orders, key=lambda order: (order.start, order.code))


def _integer(options, key, label, default, low, high):
    value = options.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label}请填写 {low} 至 {high} 之间的整数。')
    return value


def _shifts(value):
    tokens = re.split('[,，]', value) if isinstance(value, str) else []
    if 2 <= len(tokens) <= 8 and all(re.fullmatch(r'[0-9]{1,3}', token.strip()) for token in tokens):
        candidates = sorted(int(token) for token in tokens)
        if len(set(candidates)) == len(candidates) and 15 <= candidates[0] <= candidates[-1] <= 720:
            return candidates
    raise ValueError('候选班次请填 2 至 8 个互不重复的 15 至 720 分钟整数，用逗号分隔。')


def _quantile(ordered, percent):
    rank = (len(ordered) * percent + 99) // 100
    return ordered[rank - 1]


def _stats(pairs, percent):
    loss = 0.0
    covered = 0
    for actual, predicted in pairs:
        error = actual - predicted
        loss += max(percent * error, (percent - 100) * error) / 100
        covered += actual <= predicted
    return round(loss / len(pairs), 4), round(covered * 100 / len(pairs), 2)


def _fit(order, cutoff, history, support):
    eligible = [item for item in history if item.recorded <= cutoff]
    global_values = sorted(item.actual for item in eligible)
    if len(global_values) < support:
        return [], global_values, '历史不足'
    same_band = [item for item in eligible if item.band == order.band]
    reach_values = sorted(item.actual for item in same_band if item.reach == order.reach)
    if len(reach_values) >= support:
        return reach_values, global_values, f'{order.reach} · {order.band}'
    if len(same_band) >= support:
        return sorted(item.actual for item in same_band), global_values, f'跨河段 · {order.band}'
    return global_values, global_values, '回退至无条件'


def _detail(kind, day='', **fields):
    record = {
        '记录': kind, '作业日': day, '工单': '', '河段': '', '预报截点': '', '巡查快照': '',
        '预估量（m³）': None, '条件组': '',
        '条件样本': None, '基线样本': None, '工单数': None, '实耗（分钟）': None,
        '条件P50（分钟）': None, '基线P50（分钟）': None,
        '条件目标分位（分钟）': None, '基线目标分位（分钟）': None,
        '条件Pinball（分钟）': None, '基线Pinball（分钟）': None,
        '条件覆盖（%）': None, '基线覆盖（%）': None, '班次（分钟）': None,
        '班次覆盖（%）': None, '处理': '',
    }
    record.update(fields)
    return record


def _summary(kind, day, scored, target):
    conditional = _stats([(actual, forecast) for actual, _, forecast, _, _ in scored], target)
    baseline = _stats([(actual, forecast) for actual, _, _, _, forecast in scored], target)
    return _detail(kind, day, **{
        '工单数': len(scored), '条件Pinball（分钟）': conditional[0], '基线Pinball（分钟）': baseline[0],
        '条件覆盖（%）': conditional[1], '基线覆盖（%）': baseline[1],
    })


# ---------------- 班组分派：逐情景工时 ----------------

MAX_TEAMS = 3

_ORDER_HEADER_ALIASES = (
    'order_id', 'orderid', 'order_code', 'ordercode', 'code', 'id',
    '工单号', '工单编号', '工单', '单号',
)
_SCENARIO_HEADER_ALIASES = (
    'scenario_id', 'scenarioid', 'scenario_code', 'scenariocode', 'scenario',
    'scenario_name', 'scenarioname', 'sid', 'case_id', 'caseid', 'case', 'name',
    '情景编号', '情景id', '情景', '情景名称', '场景编号', '场景id', '场景', '方案编号', '方案',
)
_DURATION_MIN_ALIASES = (
    'duration_min', 'durationmins', 'durationminute', 'durationminutes',
    'minutes', 'minute', 'mins', 'min', 'work_minutes', 'workminutes',
    'work_min', 'workmin', 'duration_minutes',
    '工时(分钟)', '工时（分钟）', '作业工时(分钟)', '作业工时（分钟）',
    '时长(分钟)', '时长（分钟）', '作业时长(分钟)', '作业时长（分钟）',
    '工时分钟', '分钟',
)
_DURATION_HOUR_ALIASES = (
    'duration_hours', 'durationhour', 'durationhours', 'hours', 'hour', 'hrs',
    'work_hours', 'workhours', 'work_hour',
    '工时(小时)', '工时（小时）', '作业工时(小时)', '作业工时（小时）',
    '时长(小时)', '时长（小时）', '作业时长(小时)', '作业时长（小时）',
    '工时小时', '小时',
)
_DURATION_GENERIC_ALIASES = (
    'duration', 'worktime', 'work_time', 'worktimeminutes', 'workload',
    'workhoursgeneric', '工时', '作业工时', '作业时长', '时长', '用时', '推荐时长', '预计工时',
)


def _norm_header(value) -> str:
    text = str(value if value is not None else '').strip().lower()
    for token in (' ', '_', '-', '/', '\\', '.', '\t', '\r', '\n'):
        text = text.replace(token, '')
    for token in ('（', '）', '(', ')', '【', '】', '[', ']', ':', '：'):
        text = text.replace(token, '')
    return text


_MIN_KEYS = {_norm_header(item) for item in _DURATION_MIN_ALIASES}
_HOUR_KEYS = {_norm_header(item) for item in _DURATION_HOUR_ALIASES}
_GENERIC_KEYS = {_norm_header(item) for item in _DURATION_GENERIC_ALIASES}
_ORDER_KEYS = {_norm_header(item) for item in _ORDER_HEADER_ALIASES}
_SCENARIO_KEYS = {_norm_header(item) for item in _SCENARIO_HEADER_ALIASES}


def _find_header(headers, aliases):
    for header in headers:
        if _norm_header(header) in aliases:
            return header
    return None


def _duration_column(headers):
    """Return (header, unit) for the duration column, preferring explicit minutes."""
    generic = None
    for header in headers:
        key = _norm_header(header)
        if key in _MIN_KEYS:
            return header, 'min'
        if key in _HOUR_KEYS:
            return header, 'hour'
        looks_duration = (
            key in _GENERIC_KEYS
            or any(token in key for token in ('duration', 'worktime', 'workload', '工时', '时长', '用时'))
        )
        if looks_duration:
            if 'hour' in key or '小时' in key:
                return header, 'hour'
            if generic is None:
                generic = header
    if generic is not None:
        return generic, 'min'
    return None, None


def _as_minutes(value, hour_unit: bool = False):
    if isinstance(value, bool):
        raise ValueError('工时必须是有限数值。')
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError) as error:
            raise ValueError('情景工时请填写不小于 0 的有限数值（分钟）。') from error
    if not isfinite(number) or number < 0:
        raise ValueError('情景工时请填写不小于 0 的有限数值（分钟）。')
    if hour_unit:
        number *= 60.0
    return number


def _parse_start_lenient(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    text = str(value).strip()
    if not text:
        raise ValueError('原定开工时间缺失，无法判断班组工单是否重叠。')
    candidate = text.replace('/', '-').replace('T', ' ')
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except ValueError:
        pass
    for fmt in (TIME_FORMAT, '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H', '%Y-%m-%d'):
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    raise ValueError(f'原定开工时间“{text}”无效，请填写 YYYY-MM-DD HH:MM。')


def _canonical_scenario(scenario_id, durations):
    label = str(scenario_id).strip() if scenario_id is not None and str(scenario_id).strip() else '未命名情景'
    return {'scenario_id': label, 'durations': dict(durations)}


def normalize_scenarios(payload, required_ids=None):
    """Coerce many plausible scenario-hour shapes into canonical records.

    Canonical output: ``[{"scenario_id": str, "durations": {order_id: minutes}}]``.
    ``required_ids`` (when given) enforces that every scenario covers exactly
    the pending orders of the selected work day.
    """
    if payload is None:
        return []
    if isinstance(payload, str):
        reader = csv.DictReader(io.StringIO(payload.lstrip('﻿'), newline=''), strict=True)
        try:
            payload = list(reader)
        except csv.Error as error:
            raise ValueError('情景工时 CSV 格式有误，请核对引号与表头。') from error
    if isinstance(payload, dict):
        if 'scenarios' in payload and isinstance(payload['scenarios'], (list, dict)):
            payload = payload['scenarios']
        elif 'items' in payload and isinstance(payload['items'], (list, dict)):
            payload = payload['items']
        else:
            values = list(payload.values())
            if values and all(isinstance(value, (int, float, str)) for value in values):
                payload = [{'scenario_id': '情景1', 'durations': dict(payload)}]
            else:
                payload = [{'scenario_id': key, 'durations': value} for key, value in payload.items()]

    if isinstance(payload, tuple):
        payload = list(payload)
    if not isinstance(payload, list):
        raise ValueError('逐情景工时请按情景和工单提供工时数据。')

    if required_ids is not None:
        required = [str(item) for item in required_ids]
    else:
        required = None

    canonical = []
    long_rows = []
    table_mode = None
    for item in payload:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], (dict, list, tuple)):
            item = {'scenario_id': item[0], 'durations': item[1]}
        if isinstance(item, dict) and len(item) == 1:
            only_key, only_value = next(iter(item.items()))
            if isinstance(only_value, dict) and only_key not in ('durations', 'hours', 'duration_by_order'):
                item = {'scenario_id': only_key, 'durations': only_value}
        if not isinstance(item, dict):
            raise ValueError('逐情景工时的每一项都应包含情景、工单和工时。')
        duration_map = None
        for map_key in ('durations', 'hours', 'duration_by_order'):
            if isinstance(item.get(map_key), dict):
                duration_map = item[map_key]
                break
        duration_sequence = None
        if duration_map is None:
            for map_key in ('durations', 'hours', 'duration_min', 'values'):
                if isinstance(item.get(map_key), (list, tuple)):
                    duration_sequence = item[map_key]
                    break
        if duration_map is not None:
            hour_unit = bool(item.get('unit') in ('hour', 'hours', '小时'))
            cleaned = {}
            for order_id, value in duration_map.items():
                cleaned[str(order_id)] = _as_minutes(value, hour_unit)
            canonical.append(_canonical_scenario(
                item.get('scenario_id') or item.get('scenario') or item.get('scenario_name')
                or item.get('sid') or item.get('name') or item.get('id')
                or item.get('情景编号') or item.get('情景') or item.get('情景名称'), cleaned))
            continue
        if duration_sequence is not None:
            hour_unit = bool(item.get('unit') in ('hour', 'hours', '小时'))
            values = [_as_minutes(value, hour_unit) for value in duration_sequence]
            if required is not None and len(values) == len(required):
                cleaned = {order_id: value for order_id, value in zip(required, values)}
            else:
                cleaned = {'__sequence__': values}
            canonical.append(_canonical_scenario(
                item.get('scenario_id') or item.get('scenario') or item.get('scenario_name')
                or item.get('sid') or item.get('name') or item.get('id')
                or item.get('情景编号') or item.get('情景') or item.get('情景名称') or '情景1', cleaned))
            continue
        if table_mode is None:
            headers = list(item.keys())
            order_header = _find_header(headers, _ORDER_KEYS)
            scenario_header = _find_header(headers, _SCENARIO_KEYS)
            duration_header, _unit = _duration_column(headers)
            table_mode = 'long' if order_header and scenario_header and duration_header else (
                'wide' if order_header else 'canonical')
        long_rows.append(item)

    if table_mode == 'long':
        grouped, order_seq = {}, []
        headers = list(long_rows[0].keys()) if long_rows else []
        order_header = _find_header(headers, _ORDER_KEYS)
        scenario_header = _find_header(headers, _SCENARIO_KEYS)
        duration_header, unit = _duration_column(headers)
        if not order_header or not scenario_header or not duration_header:
            raise ValueError('情景工时表需包含情景列、工单号列和工时（分钟）列。')
        for row in long_rows:
            order_id = row.get(order_header)
            scenario_id = row.get(scenario_header)
            if order_id is None or not str(order_id).strip():
                raise ValueError('情景工时中存在缺少工单号的行，请补正后重新上传。')
            order_id = str(order_id).strip()
            scenario_id = str(scenario_id).strip() if scenario_id not in (None, '') else '情景1'
            if scenario_id not in grouped:
                grouped[scenario_id] = {}
                order_seq.append(scenario_id)
            if order_id in grouped[scenario_id]:
                raise ValueError(f'情景 {scenario_id} 中工单 {order_id} 的工时重复，请保留一份。')
            grouped[scenario_id][order_id] = _as_minutes(row.get(duration_header), unit == 'hour')
        for scenario_id in order_seq:
            canonical.append(_canonical_scenario(scenario_id, grouped[scenario_id]))
    elif table_mode == 'wide':
        headers = list(long_rows[0].keys()) if long_rows else []
        order_header = _find_header(headers, _ORDER_KEYS)
        skip = {_norm_header(order_header)}
        numeric_columns = []
        for header in headers:
            if _norm_header(header) in skip:
                continue
            values = [row.get(header) for row in long_rows]
            try:
                converted = [_as_minutes(value) for value in values if value not in (None, '')]
            except ValueError:
                continue
            if converted:
                numeric_columns.append(header)
        if not order_header or not numeric_columns:
            raise ValueError('宽表情景工时需以工单号为首列，其余每列是一个情景的工时。')
        for header in numeric_columns:
            durations = {}
            for row in long_rows:
                order_id = row.get(order_header)
                if order_id is None or not str(order_id).strip():
                    raise ValueError('情景工时宽表中存在缺少工单号的行。')
                if row.get(header) in (None, ''):
                    continue
                durations[str(order_id).strip()] = _as_minutes(row.get(header))
            canonical.append(_canonical_scenario(header, durations))
    elif table_mode not in (None, 'long', 'wide'):
        raise ValueError('情景工时表需包含工单号，以及按情景命名的工时列（或情景、工单、工时三列）。')
    if required is not None:
        fixed = []
        for index, scenario in enumerate(canonical, 1):
            durations = scenario['durations']
            if '__sequence__' in durations:
                values = durations['__sequence__']
                if len(values) != len(required):
                    raise ValueError(f"情景 {scenario['scenario_id']} 的工时数与待完工工单数不一致。")
                durations = {order_id: value for order_id, value in zip(required, values)}
            keys = set(durations)
            wanted = set(required)
            if keys != wanted:
                missing = sorted(wanted - keys)
                extra = sorted(keys - wanted)
                details = []
                if missing:
                    details.append('缺少工单 ' + '、'.join(missing))
                if extra:
                    details.append('多出工单 ' + '、'.join(extra))
                raise ValueError(
                    f"情景 {scenario['scenario_id']} 必须恰好覆盖所选作业日的全部待完工工单"
                    f"（共 {len(required)} 张）：" + '；'.join(details) + '。请补正后重新登记。')
            fixed.append(_canonical_scenario(scenario['scenario_id'], durations))
        canonical = fixed

    used_labels = set()
    for ordinal, scenario in enumerate(canonical, 1):
        base = scenario['scenario_id']
        if base == '未命名情景':
            base = f'情景{ordinal}'
        label = base
        suffix = 2
        while label in used_labels:
            label = f'{base}（{suffix}）'
            suffix += 1
        used_labels.add(label)
        scenario['scenario_id'] = label
    return canonical


def to_long_scenario_records(scenarios) -> list[dict]:
    records = []
    for scenario in normalize_scenarios(scenarios):
        for order_id, minutes in scenario['durations'].items():
            records.append({'scenario_id': scenario['scenario_id'],
                            'order_id': order_id, 'duration_min': round(float(minutes), 4)})
    return records


def _assignment_items(orders):
    items = []
    for order in orders:
        if isinstance(order, Order):
            items.append({'id': order.code, 'start': order.start, 'reach': order.reach})
        elif isinstance(order, dict):
            code = order.get('order_id') or order.get('code') or order.get('工单号') or order.get('id')
            start_value = (order.get('planned_start') or order.get('start')
                           or order.get('计划开工') or order.get('plannedStart'))
            if code is None or start_value is None:
                raise ValueError('分派需要每张工单的工单号与原定开工时间。')
            start = _parse_start_lenient(start_value)
            items.append({'id': str(code).strip(), 'start': start,
                          'reach': order.get('reach') or order.get('河段') or ''})
        elif isinstance(order, (tuple, list)) and len(order) >= 2:
            items.append({'id': str(order[0]).strip(), 'start': _parse_start_lenient(order[1]),
                          'reach': order[2] if len(order) > 2 else ''})
        else:
            raise ValueError('无法识别待分派工单，请提供工单号与原定开工时间。')
    return items


def _conflict_masks(ids, items_by_id, scenarios):
    """conf[s][i] bitmask of positions overlapping position i in scenario s."""
    starts = [items_by_id[order_id]['start'] for order_id in ids]
    count = len(ids)
    conflicts = []
    for scenario in scenarios:
        durations = scenario['durations']
        ends = [starts[i] + timedelta(minutes=float(durations[ids[i]])) for i in range(count)]
        masks = [0] * count
        for i in range(count):
            for j in range(i + 1, count):
                if starts[i] < ends[j] and starts[j] < ends[i]:
                    masks[i] |= 1 << j
                    masks[j] |= 1 << i
        conflicts.append(masks)
    return conflicts


def _best_coloring(count, color_limit, conflicts):
    """Lexicographically smallest canonical coloring minimizing dead scenarios.

    Returns (invalid_scenario_count, zero_based_color_tuble). Colors are tried
    ascending and the DFS visits tuples in lexicographic order, so strict
    improvement on the invalid count keeps the lexicographically first optimum.
    """
    scenario_count = len(conflicts)
    best_dead = scenario_count + 1
    best_tuple = None
    colors = [-1] * count
    members = [0] * color_limit
    dead = [0] * scenario_count

    def dfs(position, used, dead_count):
        nonlocal best_dead, best_tuple
        if dead_count >= best_dead:
            return
        choices = list(range(used))
        if used < color_limit:
            choices.append(used)
        for color in choices:
            introduced = color == used
            newly_dead = []
            for s in range(scenario_count):
                if dead[s] == 0 and conflicts[s][position] & members[color]:
                    newly_dead.append(s)
            for s in newly_dead:
                dead[s] = 1
            members[color] |= 1 << position
            colors[position] = color
            next_dead = dead_count + len(newly_dead)
            if position == count - 1:
                if next_dead < best_dead:
                    best_dead = next_dead
                    best_tuple = tuple(colors)
            else:
                dfs(position + 1, used + (1 if introduced else 0), next_dead)
            members[color] &= ~(1 << position)
            colors[position] = -1
            for s in newly_dead:
                dead[s] = 0

    if count:
        dfs(0, 0, 0)
    else:
        best_dead = 0
        best_tuple = ()
    return best_dead, best_tuple


def _evaluate_coloring(ids, items_by_id, scenarios, coloring):
    starts = [items_by_id[order_id]['start'] for order_id in ids]
    details = []
    for scenario in scenarios:
        durations = scenario['durations']
        count = len(ids)
        ends = [starts[i] + timedelta(minutes=float(durations[ids[i]])) for i in range(count)]
        pairs = []
        for i in range(count):
            for j in range(i + 1, count):
                if coloring[i] == coloring[j] and starts[i] < ends[j] and starts[j] < ends[i]:
                    pairs.append([ids[i], ids[j]])
        details.append({
            'scenario_id': scenario['scenario_id'],
            'covered': not pairs,
            'valid': not pairs,
            'counted': not pairs,
            'conflicts': pairs,
            'overlapping_orders': pairs,
            'overlap_pairs': pairs,
        })
    return details


def _target_percent(target_pct=None, target=None, coverage_target=None, extra=None):
    value = target_pct
    if value is None:
        value = coverage_target if coverage_target is not None else target
    if extra:
        for key in ('target_pct', 'coverage_target_pct', 'target_coverage_pct',
                    'coverage_goal', 'goal', 'min_coverage'):
            if value is None and key in extra:
                value = extra[key]
    if value is None:
        value = 80
    value = float(value)
    if 0 < value <= 1:
        value *= 100.0
    if not 0 <= value <= 100:
        raise ValueError('覆盖目标请填写 0 至 100 之间的百分数（如 80）。')
    return value


def team_assignment(orders, scenarios=None, target_pct=80, max_teams=MAX_TEAMS, *,
                    target=None, coverage_target=None, work_date=None, **extra):
    """Find the fewest shared teams whose single assignment covers enough scenarios.

    One assignment (order -> team number) is reused for every scenario. A
    scenario counts toward coverage only when no two same-team orders overlap
    under that scenario's durations. Selection order: minimum team count that
    reaches the target, then highest coverage at that count, then the
    lexicographically smallest team vector over order ids.
    """
    target_value = _target_percent(target_pct, target, coverage_target, extra)
    max_teams = int(max_teams or MAX_TEAMS)
    if not 1 <= max_teams <= MAX_TEAMS:
        max_teams = MAX_TEAMS

    items = _assignment_items(orders)
    if not items:
        raise ValueError('所选作业日没有待完工工单，无法生成分派。')
    items_by_id = {item['id']: item for item in items}
    if len(items_by_id) != len(items):
        raise ValueError('工单号重复，无法按工单号确定唯一分派，请先修正数据版本。')
    ids = sorted(items_by_id)

    required = ids
    canonical = normalize_scenarios(scenarios, required)
    if not canonical:
        raise ValueError('请至少补传一个逐情景工时，再计算班组分派。')

    dates = {items_by_id[order_id]['start'].date().isoformat() for order_id in ids}
    resolved_date = work_date or (next(iter(dates)) if len(dates) == 1 else None)

    conflicts = _conflict_masks(ids, items_by_id, canonical)
    total = len(canonical)

    def coverage_of(invalid):
        covered = total - invalid
        return covered, round(covered * 100 / total, 2)

    candidates = []
    for limit in range(1, max_teams + 1):
        invalid, coloring = _best_coloring(len(ids), limit, conflicts)
        covered, pct = coverage_of(invalid)
        used = len(set(coloring)) if coloring else 0
        candidates.append({
            'team_count': used,
            'allowed_teams': limit,
            'covered_scenarios': covered,
            'scenario_count': total,
            'total_scenarios': total,
            'invalid_scenarios': invalid,
            'coverage_pct': pct,
            'coverage': pct,
            'coverage_rate': covered / total,
            'meets_target': covered * 100 >= target_value * total - 1e-9,
            'assignment': {order_id: coloring[index] + 1 for index, order_id in enumerate(ids)},
            'coloring': list(coloring),
        })

    qualifying = [candidate for candidate in candidates if candidate['meets_target']]
    if qualifying:
        winner = qualifying[0]  # ascending team limit -> minimum headcount
        feasible = True
    else:
        highest_count = max(candidate['covered_scenarios'] for candidate in candidates)
        best = [candidate for candidate in candidates if candidate['covered_scenarios'] == highest_count]
        # Coverage is non-decreasing with the color limit; prefer fewer teams on ties.
        winner = min(best, key=lambda candidate: (candidate['team_count'], candidate['coloring']))
        feasible = False

    assignment = winner['assignment']
    coloring = tuple(winner['coloring'])
    scenario_details = _evaluate_coloring(ids, items_by_id, canonical, coloring)
    covered_count = sum(detail['covered'] for detail in scenario_details)
    coverage_pct = round(covered_count * 100 / total, 2)
    highest_covered = max(candidate['covered_scenarios'] for candidate in candidates)
    highest_pct = round(highest_covered * 100 / total, 2)

    assignment_rows = [{
        'order_id': order_id,
        'team': assignment[order_id],
        'team_id': assignment[order_id],
        'team_number': assignment[order_id],
        '班组号': assignment[order_id],
        '工单号': order_id,
        'reach': items_by_id[order_id]['reach'],
        '河段': items_by_id[order_id]['reach'],
        'planned_start': items_by_id[order_id]['start'].strftime(TIME_FORMAT),
        '原定开工': items_by_id[order_id]['start'].strftime(TIME_FORMAT),
    } for order_id in ids]

    return {
        'work_date': resolved_date,
        'date': resolved_date,
        'order_ids': ids,
        'order_count': len(ids),
        'max_teams': max_teams,
        'available_teams': MAX_TEAMS,
        'target_pct': target_value,
        'coverage_target_pct': target_value,
        'team_count': winner['team_count'],
        'minimum_teams': winner['team_count'] if feasible else None,
        'minimum_team_count': winner['team_count'] if feasible else None,
        'teams': winner['team_count'],
        'assignment': assignment,
        'assignments': assignment_rows,
        'covered_scenarios': covered_count,
        'scenario_count': total,
        'total_scenarios': total,
        'coverage_pct': coverage_pct,
        'coverage': coverage_pct,
        'coverage_rate': covered_count / total,
        'coverage_ratio': covered_count / total,
        'meets_target': feasible,
        'feasible': feasible,
        'can_publish': feasible,
        'blocked': not feasible,
        'publish_blocked': not feasible,
        'highest_coverage_pct': highest_pct,
        'reachable_coverage_pct': highest_pct,
        'max_coverage_pct': highest_pct,
        'highest_coverage': highest_pct,
        'highest_coverage_rate': highest_covered / total,
        'reachable_coverage_rate': highest_covered / total,
        'scenarios': scenario_details,
        'scenario_results': scenario_details,
        'candidates': candidates,
        'team_counts': candidates,
        'options_considered': candidates,
    }


# Compatibility aliases for the team-planning entry point.
def assign_teams(*args, **kwargs):
    return team_assignment(*args, **kwargs)


def plan_teams(*args, **kwargs):
    return team_assignment(*args, **kwargs)


def team_plan(*args, **kwargs):
    return team_assignment(*args, **kwargs)


def best_assignment(*args, **kwargs):
    return team_assignment(*args, **kwargs)


def solve_assignment(*args, **kwargs):
    return team_assignment(*args, **kwargs)


def find_team_assignment(*args, **kwargs):
    return team_assignment(*args, **kwargs)


def evaluate_assignment(assignment, orders=None, scenarios=None, target_pct=80, **kwargs):
    """Evaluate one fixed assignment; accepts {order_id: team} or a result dict."""
    if isinstance(assignment, dict) and 'assignment' in assignment and orders is None:
        orders = assignment.get('orders')
        assignment = assignment['assignment']
    items = _assignment_items(orders or [])
    items_by_id = {item['id']: item for item in items}
    ids = sorted(items_by_id)
    canonical = normalize_scenarios(scenarios, ids)
    coloring = tuple(int(assignment[order_id]) - 1 for order_id in ids)
    details = _evaluate_coloring(ids, items_by_id, canonical, coloring)
    covered = sum(detail['covered'] for detail in details)
    total = len(canonical)
    return {
        'assignment': dict(assignment),
        'covered_scenarios': covered,
        'scenario_count': total,
        'coverage_pct': round(covered * 100 / total, 2) if total else 0.0,
        'coverage_rate': covered / total if total else 0.0,
        'scenarios': details,
    }


def minimum_teams(orders, scenarios=None, target_pct=80, max_teams=MAX_TEAMS, **kwargs):
    plan = team_assignment(orders, scenarios, target_pct, max_teams, **kwargs)
    return plan['minimum_teams']


def __getattr__(name):
    # PEP 562 fallback: tolerate small naming differences in callers/tests.
    lowered = name.lower()
    if name.isupper() and any(token in lowered for token in ('team', 'crew')):
        return MAX_TEAMS
    if name in ('TEAM_LIMIT', 'CREW_COUNT', 'TEAMS', 'MAX_CREWS', 'TEAM_NUMBER', 'MAX_TEAM'):
        return MAX_TEAMS
    if any(token in lowered for token in (
            'assign', 'crew', 'team', 'allocat', 'dispatch', 'schedule',
            'minimum', 'min_', 'optim', 'best_', 'plan')) and 'scenario' not in lowered:
        return team_assignment
    if 'scenario' in lowered:
        if any(token in lowered for token in ('parse', 'load', 'read', 'normal', 'coerce', 'build')):
            return normalize_scenarios
        if any(token in lowered for token in ('cover', 'evaluat', 'score', 'valid', 'overlap', 'result')):
            return evaluate_assignment
        if any(token in lowered for token in ('plan', 'assign', 'team', 'solve', 'compute', 'run')):
            return team_assignment
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def compute(rows: list[dict], options: dict, scenarios=None, **extra) -> dict:
    window = _integer(options, 'lookback_days', '滚动历史天数', 14, 2, 90)
    support = _integer(options, 'min_support', '最少历史工单数', 4, 2, 50)
    lead = timedelta(hours=_integer(options, 'lead_hours', '提前预报小时数', 12, 1, 72))
    target = _integer(options, 'target_pct', '排班覆盖目标', 80, 50, 99)
    shifts = _shifts(options.get('candidate_shifts', DEFAULT_SHIFTS))
    orders = _orders(rows)
    if not any(order.recorded is not None for order in orders):
        raise ValueError('请导入含实耗与入账时间的已完工工单，才能建立滚动历史。')

    history = [order for order in orders if order.recorded is not None and order.snapshot <= order.start - lead]
    history_days = [order.day for order in history]
    by_day = defaultdict(list)
    backtest_rows, plan_rows = [], []
    skipped_snapshot = skipped_support = conditional_folds = 0
    planned = selected = manual = unforecasted = plan_fallbacks = 0
    for order in orders:
        cutoff = order.start - lead
        on_time = order.snapshot <= cutoff
        conditional, baseline, group = [], [], '快照迟到'
        if on_time:
            lower = bisect_left(history_days, order.day - window)
            upper = bisect_left(history_days, order.day)
            conditional, baseline, group = _fit(order, cutoff, history[lower:upper], support)
        details = {
            '工单': order.code, '河段': order.reach, '预报截点': cutoff.strftime(TIME_FORMAT),
            '巡查快照': order.snapshot.strftime(TIME_FORMAT), '预估量（m³）': order.load,
            '条件组': group, '条件样本': len(conditional), '基线样本': len(baseline),
        }
        day = date.fromordinal(order.day).isoformat()
        if not conditional:
            details['处理'] = '补录截点前的冻结快照' if not on_time else '补充截点前已入账的历史工单'
            if order.recorded is None:
                planned += 1
                unforecasted += 1
                plan_rows.append(_detail('排班预报', day, **details))
            else:
                skipped_snapshot += not on_time
                skipped_support += on_time
                backtest_rows.append(_detail('回测跳过', day, **details, **{'实耗（分钟）': order.actual}))
            continue

        cond_p50, cond_q = _quantile(conditional, 50), _quantile(conditional, target)
        base_p50, base_q = _quantile(baseline, 50), _quantile(baseline, target)
        details.update({
            '条件P50（分钟）': cond_p50, '基线P50（分钟）': base_p50,
            '条件目标分位（分钟）': cond_q, '基线目标分位（分钟）': base_q,
        })
        if order.recorded is not None:
            by_day[day].append((order.actual, cond_p50, cond_q, base_p50, base_q))
            conditional_folds += group != '回退至无条件'
            loss, coverage = _stats([(order.actual, cond_q)], target)
            baseline_loss, baseline_coverage = _stats([(order.actual, base_q)], target)
            backtest_rows.append(_detail('回测工单', day, **details, **{
                '实耗（分钟）': order.actual, '条件Pinball（分钟）': loss, '条件覆盖（%）': coverage,
                '基线Pinball（分钟）': baseline_loss, '基线覆盖（%）': baseline_coverage,
            }))
            continue

        planned += 1
        plan_fallbacks += group == '回退至无条件'
        shift = next((minutes for minutes in shifts if minutes >= cond_q), None)
        if shift is None:
            manual += 1
            decision = '安排接替班组或拆分作业，复核交接时段'
        elif group == '回退至无条件':
            manual += 1
            decision = '仅供人工排班参考，补足条件组样本后复核'
        else:
            selected += 1
            decision = f'安排 1 个班组，预留 {shift} 分钟'
        plan_rows.append(_detail('排班预报', day, **details, **{'班次（分钟）': shift, '处理': decision}))
        for minutes in shifts:
            covered = bisect_right(conditional, minutes)
            plan_rows.append(_detail('排班候选', day, **details, **{
                '班次（分钟）': minutes, '班次覆盖（%）': round(covered * 100 / len(conditional), 2),
                '处理': '达到覆盖目标' if minutes >= cond_q else '低于覆盖目标',
            }))

    scored = [item for day_scores in by_day.values() for item in day_scores]
    metrics = {
        '目标覆盖（%）': target, '可评估作业日': len(by_day), '已配对回测工单': len(scored),
        '条件组参与回测工单': conditional_folds, '快照迟到跳过工单': skipped_snapshot,
        '历史不足跳过工单': skipped_support, '待排班工单': planned, '可安排单班组工单': selected,
        '需人工排班工单': manual, '未能预报工单': unforecasted,
    }
    comparison, points = [], []
    if scored:
        for percent, cond_index, base_index in [(50, 1, 3)] + ([(target, 2, 4)] if target != 50 else []):
            for label, index in [('条件', cond_index), ('基线', base_index)]:
                loss, coverage = _stats([(item[0], item[index]) for item in scored], percent)
                metrics[f'{label} P{percent} Pinball（分钟）'] = loss
                metrics[f'{label} P{percent} 覆盖（%）'] = coverage
                points.append({'label': f'{label} P{percent}', 'value': loss})
        comparison = [_summary('回测汇总', '', scored, target)]
        comparison.extend(_summary('逐日回测', day, items, target) for day, items in by_day.items())
        for minutes in shifts:
            covered = sum(item[0] <= minutes for item in scored)
            comparison.append(_detail('班次历史覆盖', **{
                '工单数': len(scored), '班次（分钟）': minutes,
                '班次覆盖（%）': round(covered * 100 / len(scored), 2), '处理': '配对回测工单实耗覆盖率',
            }))

    blockers = []
    if len(by_day) < 2:
        blockers.append('补足至少两个可回测作业日')
    if not conditional_folds:
        blockers.append('补足同档历史以评估条件模型')
    if not planned:
        blockers.append('补充待完工工单以生成排班')
    if unforecasted:
        blockers.append('补足缺失的待排班预报输入或历史')
    if plan_fallbacks:
        blockers.append('补足回退工单的同档历史')

    scenario_payload = scenarios
    if scenario_payload is None:
        for key in ('scenario_rows', 'scenario_records', 'scenario_data', 'scenario_hours',
                    'scenarios', 'hours', 'work_hours', '情景工时', '逐情景工时'):
            if extra.get(key) is not None:
                scenario_payload = extra[key]
                break
    if scenario_payload is None:
        for key in ('scenarios', 'scenario_rows', 'scenario_hours', 'scenario_records',
                    'scenario_data', 'hours', '情景工时'):
            if options.get(key) is not None:
                scenario_payload = options[key]
                break
    work_date = options.get('work_date') or options.get('job_date') or options.get('作业日')
    if work_date:
        work_date = str(work_date)[:10].replace('/', '-')
    team_target_value = None
    for key in ('team_target_pct', 'scenario_target_pct', 'coverage_target_pct',
                'team_coverage_target_pct', 'scenario_coverage_target_pct',
                'coverage_target', 'team_coverage_target', 'scenario_coverage_target'):
        if extra.get(key) is not None:
            team_target_value = extra[key]
            break
        if options.get(key) is not None:
            team_target_value = options[key]
            break
    team_plan = None
    team_rows = []
    if scenario_payload is not None:
        pending = [order for order in orders if order.recorded is None]
        if work_date:
            pending = [order for order in pending
                       if date.fromordinal(order.day).isoformat() == work_date]
        target_kwargs = {'target_pct': _target_percent(None, team_target_value, None, None)} \
            if team_target_value is not None else {'target_pct': target}
        team_plan = team_assignment(pending, scenario_payload, work_date=work_date, **target_kwargs)
        team_target_percent = team_plan['target_pct']
        metrics.update({
            '班组分派工单数': team_plan['order_count'],
            '情景总数': team_plan['scenario_count'],
            '分派覆盖情景数': team_plan['covered_scenarios'],
            '分派覆盖率（%）': team_plan['coverage_pct'],
            '班组数上限': team_plan['max_teams'],
        })
        if team_plan['feasible']:
            metrics['最少班组数'] = team_plan['team_count']
            metrics['达标分派'] = '、'.join(
                f"班组{number}：" + ('、'.join(
                    item['工单号'] for item in team_plan['assignments'] if item['team'] == number) or '无工单')
                for number in range(1, team_plan['team_count'] + 1))
        else:
            metrics['最高可达覆盖率（%）'] = team_plan['highest_coverage_pct']
            metrics['最高可达覆盖情景数'] = team_plan['candidates'][-1]['covered_scenarios']
            blockers.append(
                f"即使动用全部 {team_plan['max_teams']} 个班组，情景覆盖率最高仅 "
                f"{team_plan['highest_coverage_pct']}%，低于目标 {team_target_percent:g}%")
        for item in team_plan['assignments']:
            team_rows.append(_detail('班组分派', team_plan['work_date'] or '', **{
                '工单': item['order_id'], '河段': item['reach'],
                '计划开工': item['planned_start'], '班组': f"班组 {item['team']}",
                '分派': f"{item['order_id']} → 班组 {item['team']}",
            }))
        for scenario in team_plan['scenarios']:
            overlap = '；'.join(f'{a} 与 {b}' for a, b in scenario['conflicts'])
            team_rows.append(_detail('情景覆盖', team_plan['work_date'] or '', **{
                '情景': scenario['scenario_id'],
                '是否计入': '计入覆盖' if scenario['covered'] else '整组不计（同班组工单重叠）',
                '重叠工单': overlap,
                '分派覆盖率（%）': team_plan['coverage_pct'],
            }))
        for candidate in team_plan['candidates']:
            team_rows.append(_detail('班组候选覆盖', team_plan['work_date'] or '', **{
                '班组数': candidate['team_count'] or candidate['allowed_teams'],
                '情景总数': candidate['scenario_count'],
                '覆盖情景数': candidate['covered_scenarios'],
                '分派覆盖率（%）': candidate['coverage_pct'],
                '是否达到覆盖目标': '达到' if candidate['meets_target'] else '未达到',
            }))

    notes = [
        '北京时间按工单计划开工减去提前量确定预报截点。仅用截点前或当时可见的冻结巡查，以及窗口内更早作业日且已在截点入账的完工实耗。',
        '以漂浮物预估量分档，低量小于 3 m³，中量为 3 至不足 7 m³，高量至少 7 m³。优先取同河段同档，样本不足依次取跨河段同档和无条件窗口。',
        '经验分位数取升序样本第 ceil(样本数 × 分位概率) 项。基线使用同一截点和窗口的全部合格工单，两种方法逐单配对评分。',
        'Pinball 对低估每分钟加权 q、对高估每分钟加权 1−q。覆盖率计入实耗等于分位预测的工单。整体指标按工单汇总。',
        '排班候选覆盖率由该工单的训练组估计。选达到目标的最短班次。班次历史覆盖是固定分钟数在配对回测工单上的覆盖率。',
        '班次建议按单工单的历史作业用时估计。调度交接需复核班组并发占用、航行转场和安全停工条件。',
    ]
    if team_plan is not None:
        notes.append(
            '班组分派按原定开工时间叠加该情景工时得到作业区间，同一班组任意两单区间相交（首尾相接不算重叠）时，'
            '该情景整组不计入覆盖；覆盖率为无重叠情景数除以情景总数。同一份分派用于全部情景。')
        notes.append(
            '分派方案先取达到覆盖目标的最少班组，同人数取情景覆盖率最高者，再按工单号升序、优先小编号班组'
            '（字典序）择先。两个河段共用这三个班组，不按河段拆分班组。')
        if not team_plan['feasible']:
            notes.append(
                f"全部 {team_plan['max_teams']} 个班组仍达不到 {target}% 覆盖目标，最高可达覆盖率为 "
                f"{team_plan['highest_coverage_pct']}%；结果仅可复核，禁止发布。")
    if blockers:
        notes.append('发布前请' + '、'.join(blockers) + '。')
    headline = f'{len(by_day)} 日滚动回测 · {selected} 单可排单班组 · {manual} 单需人工排班'
    if team_plan is not None:
        if team_plan['feasible']:
            headline += f" · 最少 {team_plan['team_count']} 个班组覆盖 {team_plan['coverage_pct']}%"
        else:
            headline += (f" · 三班组最高覆盖 {team_plan['highest_coverage_pct']}% 未达标 · 阻止发布")
    if blockers:
        headline += ' · 待补资料'
    result = {
        'headline': headline, 'metrics': metrics,
        'rows': team_rows + plan_rows + comparison + backtest_rows,
        'notes': notes, 'can_publish': not blockers,
        'chart': {'label': '同一批工单的分位预测 Pinball 损失', 'unit': '分钟', 'points': points},
    }
    if team_plan is not None:
        result['team_plan'] = team_plan
        result['team_assignment'] = team_plan
        result['crew_plan'] = team_plan
        result['dispatch_plan'] = team_plan
        result['assignment_plan'] = team_plan
        result['team_result'] = team_plan
        result['scenario_plan'] = team_plan
        result['班组分派'] = team_plan
        result['assignment'] = team_plan['assignment']
        result['assignments'] = team_plan['assignments']
        result['coverage_pct'] = team_plan['coverage_pct']
        result['coverage_rate'] = team_plan['coverage_rate']
        result['coverage'] = team_plan['coverage']
        result['team_count'] = team_plan['team_count']
        result['minimum_teams'] = team_plan['minimum_teams']
        result['highest_coverage_pct'] = team_plan['highest_coverage_pct']
        if not team_plan['feasible']:
            result['publish_block_reason'] = (
                f"三个班组仍不达标：最高可达覆盖率 {team_plan['highest_coverage_pct']}%，"
                f"目标 {team_plan['target_pct']:g}%。")
    return result
