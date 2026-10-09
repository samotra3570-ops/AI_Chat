"""공식 Costs API의 읽기 전용 요약. 충전 잔액을 계산하거나 반환하지 않는다."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import re
import time

import httpx


class OpenAIBilling:
    def __init__(self, admin_key=None, project_id=None, *, client=None, clock=time.time):
        self._key = admin_key.strip() if admin_key else None
        if project_id and not re.fullmatch(r'proj_[A-Za-z0-9_-]{1,150}', project_id):
            raise ValueError('Invalid billing project configuration')
        self._project = project_id
        self._http = client or (httpx.AsyncClient(timeout=10, follow_redirects=False) if self._key else None)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cached = None
        self._expires = 0
        self._day = None

    async def aclose(self):
        if self._http is not None:
            await self._http.aclose()

    def _result(self, status, now):
        return {'provider': 'openai', 'status': status, 'balance': None,
                'balance_status': 'official_page_only', 'checked_at': int(now),
                'scope': 'project' if self._project else 'organization', 'timezone': 'UTC'}

    async def summary(self):
        try:
            return await asyncio.wait_for(self._summary(), timeout=16)
        except TimeoutError:
            return self._result('unavailable', self._clock())

    async def _summary(self):
        async with self._lock:
            now = self._clock()
            if self._cached is not None and now < self._expires and self._day == int(now // 86400):
                return self._cached
            if not self._key:
                result = self._result('not_configured', now)
            else:
                try:
                    result = await asyncio.wait_for(self._fetch(now), timeout=15)
                except PermissionError:
                    result = self._result('unauthorized', now)
                except Exception:
                    # 인증 값, 공급자 응답 본문, 예외 문자열은 앱과 로그에 전달하지 않는다.
                    result = self._result('unavailable', now)
            self._cached = result
            self._day = int(now // 86400)
            self._expires = now + (60 if result['status'] == 'available' else 15)
            return result

    async def _fetch(self, now):
        current = datetime.fromtimestamp(now, timezone.utc)
        month = int(current.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        today = int(current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        end = int(now)
        params = {'start_time': month, 'end_time': end, 'bucket_width': '1d', 'limit': 31}
        if self._project:
            params['project_ids'] = [self._project]
        daily, monthly, seen, buckets = Decimal(0), Decimal(0), set(), set()
        for _ in range(6):
            # 목적지와 조회 범위는 서버가 고정한다. 앱 입력으로 URL/헤더를 바꿀 수 없다.
            async with self._http.stream('GET', 'https://api.openai.com/v1/organization/costs',
                    params=params, headers={'Authorization': 'Bearer ' + self._key}) as response:
                if response.status_code in (401, 403):
                    raise PermissionError()
                if response.status_code != 200:
                    raise ValueError('Billing unavailable')
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise ValueError('Invalid billing response')
            value = json.loads(raw, parse_float=Decimal)
            data = value.get('data')
            if value.get('object') != 'page' or not isinstance(data, list) or len(data) > 100:
                raise ValueError('Invalid billing response')
            for bucket in data:
                start, finish, rows = bucket.get('start_time'), bucket.get('end_time'), bucket.get('results')
                if (type(start) is not int or type(finish) is not int or start < month or
                        start >= end or finish <= start or finish > start + 86400 or
                        start in buckets or not isinstance(rows, list) or len(rows) > 1000):
                    raise ValueError('Invalid billing bucket')
                buckets.add(start)
                for row in rows:
                    if row.get('object') != 'organization.costs.result':
                        raise ValueError('Invalid billing row')
                    amount = row.get('amount')
                    if not isinstance(amount, dict) or amount.get('currency') != 'usd':
                        raise ValueError('Invalid billing currency')
                    number = amount.get('value')
                    if isinstance(number, bool) or not isinstance(number, (int, Decimal)):
                        raise ValueError('Invalid billing amount')
                    charge = Decimal(number)
                    if not charge.is_finite() or abs(charge) > 1000000:
                        raise ValueError('Invalid billing amount')
                    monthly += charge
                    if start >= today:
                        daily += charge
            if type(value.get('has_more')) is not bool:
                raise ValueError('Invalid billing page')
            if not value['has_more']:
                break
            page = value.get('next_page')
            if not isinstance(page, str) or not 1 <= len(page) <= 500 or page in seen:
                raise ValueError('Invalid billing cursor')
            seen.add(page)
            params['page'] = page
        else:
            raise ValueError('Billing page limit')
        if abs(monthly) > 1000000 or abs(daily) > 1000000:
            raise ValueError('Invalid billing total')
        result = self._result('available', now)
        result.update(currency='USD', day_usd=format(daily.quantize(Decimal('.000001'), rounding=ROUND_HALF_UP), 'f'),
                      month_usd=format(monthly.quantize(Decimal('.000001'), rounding=ROUND_HALF_UP), 'f'),
                      period_start=month, through=end)
        return result
