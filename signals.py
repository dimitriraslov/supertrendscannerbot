"""Pure closed-candle signal calculations shared by Telegram and the dashboard."""
from supertrend import supertrend, label

PIVOT_LEFT = PIVOT_RIGHT = 5
MIN_PIVOT_GAP, MAX_PIVOT_GAP = 5, 50
MIN_RSI_DIFF = 3.0


def calc_rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(v, 0) for v in changes[:period]) / period
    loss = sum(max(-v, 0) for v in changes[:period]) / period
    for i in range(period, len(closes)):
        if i > period:
            gain = (gain * (period - 1) + max(changes[i - 1], 0)) / period
            loss = (loss * (period - 1) + max(-changes[i - 1], 0)) / period
        out[i] = 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def pivot(values, i, kind):
    if i < PIVOT_LEFT or i + PIVOT_RIGHT >= len(values):
        return False
    neighbors = values[i - PIVOT_LEFT:i] + values[i + 1:i + PIVOT_RIGHT + 1]
    if values[i] is None or any(x is None for x in neighbors):
        return False
    return values[i] < min(neighbors) if kind == 'bullish' else values[i] > max(neighbors)


def divergences_at(closes, rsi, i):
    """Compare consecutive confirmed price pivots and RSI AT those same pivots.

    The second pivot becomes known only after five right-hand bars have closed.
    Using identical timestamps for price and RSI avoids matching unrelated swings.
    """
    second = i - PIVOT_RIGHT
    found = []
    for direction in ('bullish', 'bearish'):
        if not pivot(closes[:i + 1], second, direction):
            continue
        first = next((j for j in range(second - 1, max(PIVOT_LEFT - 1, second - MAX_PIVOT_GAP - 1), -1)
                      if pivot(closes[:i + 1], j, direction)), None)
        if first is None or second - first < MIN_PIVOT_GAP:
            continue
        r1, r2 = rsi[first], rsi[second]
        if r1 is None or r2 is None:
            continue
        p1, p2 = closes[first], closes[second]
        bullish = direction == 'bullish'
        valid = (p2 < p1 and r2 - r1 >= MIN_RSI_DIFF and max(r1, r2) < 40) if bullish else (
            p2 > p1 and r1 - r2 >= MIN_RSI_DIFF and min(r1, r2) > 60)
        if valid:
            found.append(dict(kind='divergence', direction=direction, pivot1_index=first,
                              pivot2_index=second, price1=p1, price2=p2, rsi1=r1, rsi2=r2,
                              strength='strong' if abs(r2 - r1) >= 6 else 'moderate'))
    return found


def events_at(closes, rsi, directions, i):
    """All independent conditions; no RSI-oversold gate on other signals."""
    out = []
    if i < 1:
        return out
    cur, prev = rsi[i], rsi[i - 1]
    if directions[i] is not None and directions[i - 1] is not None and directions[i] != directions[i - 1]:
        out.append(dict(kind='flip', direction=label(directions[i])))
    if cur is not None and prev is not None:
        for level in (30, 20):
            if prev >= level and cur < level:
                out.append(dict(kind='oversold', direction='bullish', level=level, rsi=cur, previous_rsi=prev))
        # Require the trend to exist on both sides of the cross.
        if directions[i] is not None and directions[i] == directions[i - 1]:
            if label(directions[i]) == 'bull' and prev > 50 >= cur:
                out.append(dict(kind='pullback', direction='buy', rsi=cur, previous_rsi=prev))
            elif label(directions[i]) == 'bear' and prev < 50 <= cur:
                out.append(dict(kind='pullback', direction='sell', rsi=cur, previous_rsi=prev))
    out.extend(divergences_at(closes, rsi, i))
    return out


def calculate(bars):
    closes = [b['c'] for b in bars]
    st = supertrend([b['h'] for b in bars], [b['l'] for b in bars], closes, 10, 3.0)
    return closes, calc_rsi(closes), st
