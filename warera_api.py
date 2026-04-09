import asyncio
import os
import re
import aiohttp
from typing import Optional, Dict, List

BASE = 'https://api2.warera.io/trpc'
HEADERS = {'accept': '*/*', 'Content-Type': 'application/json'}

_API_KEY: Optional[str] = os.getenv('WARERA_API_KEY') or None


def set_api_key(key: Optional[str]):
    global _API_KEY
    _API_KEY = key.strip() if key and key.strip() else None


def extract_user_id(text: str) -> Optional[str]:
    """Extract a 24-char hex MongoDB ObjectId from a URL or raw input."""
    match = re.search(r'[0-9a-f]{24}', text.strip(), re.IGNORECASE)
    return match.group(0).lower() if match else None


async def _post(endpoint: str, payload: dict) -> Optional[dict]:
    headers = dict(HEADERS)
    if _API_KEY:
        headers['x-api-key'] = _API_KEY
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f'{BASE}/{endpoint}', json=payload, headers=headers
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get('result', {}).get('data')
    return None


async def _batch_post(calls: list) -> list:
    """Send multiple tRPC calls in a single HTTP request (?batch=1)."""
    if not calls:
        return []
    if len(calls) == 1:
        return [await _post(calls[0][0], calls[0][1])]
    headers = dict(HEADERS)
    if _API_KEY:
        headers['x-api-key'] = _API_KEY
    path = ','.join(ep for ep, _ in calls)
    body = {str(i): payload for i, (_, payload) in enumerate(calls)}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f'{BASE}/{path}?batch=1', json=body, headers=headers
        ) as resp:
            if resp.status != 200:
                return [None] * len(calls)
            data = await resp.json()
            if not isinstance(data, list):
                return [None] * len(calls)
            return [
                item.get('result', {}).get('data') if isinstance(item, dict) else None
                for item in data
            ]


async def batch_get_user_lite(user_ids: list, chunk_size: int = 50) -> list:
    """Fetch multiple users via tRPC batch, in chunks of chunk_size."""
    results: list = []
    for i in range(0, len(user_ids), chunk_size):
        chunk = user_ids[i:i + chunk_size]
        calls = [('user.getUserLite', {'userId': uid}) for uid in chunk]
        results.extend(await _batch_post(calls))
        if i + chunk_size < len(user_ids):
            await asyncio.sleep(0.3)
    return results


async def get_user_lite(user_id: str) -> Optional[Dict]:
    return await _post('user.getUserLite', {'userId': user_id})


async def get_country_by_id(country_id: str) -> Optional[Dict]:
    return await _post('country.getCountryById', {'countryId': country_id})


async def get_user_company_ids(user_id: str) -> List[str]:
    data = await _post('company.getCompanies', {'userId': user_id, 'perPage': 12})
    if data:
        return data.get('items', [])
    return []


async def get_company(company_id: str) -> Optional[Dict]:
    return await _post('company.getById', {'companyId': company_id})


async def get_users_by_country(country_id: str, cursor: str = None) -> Optional[Dict]:
    """Returns {'items': [...], 'nextCursor': str|None}"""
    payload: dict = {'countryId': country_id, 'limit': 100}
    if cursor:
        payload['cursor'] = cursor
    return await _post('user.getUsersByCountry', payload)


async def get_government_by_country_id(country_id: str) -> Optional[Dict]:
    """Returns {president, vicePresident, minOfForeignAffairs, minOfEconomy,
    minOfDefense, congressMembers (list)} as WarEra user IDs."""
    return await _post('government.getByCountryId', {'countryId': country_id})


async def batch_get_government_by_country_ids(country_ids: list) -> dict:
    """Fetch government data for multiple countries in one batch request.
    Returns {country_id: govt_data} for countries where the API succeeded."""
    if not country_ids:
        return {}
    calls = [('government.getByCountryId', {'countryId': cid}) for cid in country_ids]
    results = await _batch_post(calls)
    return {cid: data for cid, data in zip(country_ids, results) if data}


def get_government_role_from_govt_data(user_warera_id: str, country_id: str, govt_data: dict) -> tuple:
    """Returns (role_field, access_level, country_id) — same shape as get_government_role —
    but using government.getByCountryId data instead of getUserLite.infos.
    role_field uses the …Of naming convention for compatibility with role_display_name etc.
    Returns (None, None, None) if the user holds no role or govt_data is unavailable."""
    if not user_warera_id or not govt_data:
        return None, None, None
    for gf, rf in [('president', 'presidentOf'), ('vicePresident', 'vicePresidentOf'),
                   ('minOfForeignAffairs', 'minOfForeignAffairsOf')]:
        if govt_data.get(gf) == user_warera_id:
            return rf, 'write', country_id
    for gf, rf in [('minOfEconomy', 'minOfEconomyOf'), ('minOfDefense', 'minOfDefenseOf')]:
        if govt_data.get(gf) == user_warera_id:
            return rf, 'read', country_id
    congress = govt_data.get('congressMembers', [])
    if isinstance(congress, list) and user_warera_id in congress:
        return 'congressMemberOf', 'read', country_id
    return None, None, None


async def get_company_names(user_id: str) -> List[str]:
    company_ids = await get_user_company_ids(user_id)
    names = []
    for cid in company_ids:
        company = await get_company(cid)
        if company and company.get('name'):
            names.append(company['name'])
    return names


# Skill keys from getUserLite that are considered economic (eco) builds.
# All other skills (attack, armor, criticalChance, etc.) are war skills.
ECO_SKILLS = frozenset({'entrepreneurship', 'energy', 'production', 'companies', 'management'})


def classify_player_build(skills: dict) -> str:
    """Classify a player's build as 'eco', 'war', 'hybrid', or 'uncategorized'.

    Uses the ``skills`` dict from getUserLite where each value has a ``level`` int.
    Eco skills: entrepreneurship, energy, production, companies, management.
    Everything else counts as a war skill.
    """
    if not skills or not isinstance(skills, dict):
        return 'uncategorized'
    eco = sum(
        v.get('level', 0) for k, v in skills.items()
        if k in ECO_SKILLS and isinstance(v, dict)
    )
    war = sum(
        v.get('level', 0) for k, v in skills.items()
        if k not in ECO_SKILLS and isinstance(v, dict)
    )
    total = eco + war
    if total == 0:
        return 'uncategorized'
    ratio = eco / total
    if ratio >= 0.7:
        return 'eco'
    if ratio <= 0.3:
        return 'war'
    return 'hybrid'

# Mapping of (govt_field, db_config_key, display_name) for home-country government roles.
# govt_field matches the keys from government.getByCountryId:
#   president/vicePresident/minOf* → single user-ID string
#   congressMembers                → list of user-ID strings
# The WarEra government structure is universal — the same fields apply to every country.
LOCAL_ROLES = [
    ('president',          'local_role_president_id',       'President'),
    ('vicePresident',      'local_role_vice_president_id',  'Vice President'),
    ('minOfForeignAffairs','local_role_mfa_id',             'Minister of Foreign Affairs'),
    ('minOfEconomy',       'local_role_economy_id',         'Minister of Economy'),
    ('minOfDefense',       'local_role_defense_id',         'Minister of Defense'),
    ('congressMembers',    'local_role_congress_id',        'Congress Member'),
]


def get_government_role(infos: dict) -> tuple:
    """
    Returns (role_field, access_level, country_id).
    access_level is 'write' for high officials, 'read' for others, None if no role.
    """
    high_roles = ['presidentOf', 'vicePresidentOf', 'minOfForeignAffairsOf']
    for field in high_roles:
        if infos.get(field):
            return field, 'write', infos[field]

    # Other ministerial roles — read only
    for key, value in infos.items():
        if key.startswith('minOf') and key not in high_roles and value:
            return key, 'read', value

    if infos.get('congressMemberOf'):
        return 'congressMemberOf', 'read', infos['congressMemberOf']

    return None, None, None


def role_display_name(role_field: str) -> str:
    mapping = {
        'presidentOf': 'President',
        'vicePresidentOf': 'Vice President',
        'minOfForeignAffairsOf': 'Minister of Foreign Affairs',
        'congressMemberOf': 'Congress Member',
    }
    if role_field in mapping:
        return mapping[role_field]
    if role_field.startswith('minOf'):
        inner = role_field[5:]
        if inner.endswith('Of'):
            inner = inner[:-2]
        words = re.sub(r'([A-Z])', r' \1', inner).strip()
        return f'Minister of {words}'
    return role_field


def get_all_roles_display(infos: dict) -> str:
    """Return a human-readable string of all detected government roles."""
    roles = []
    all_role_fields = [
        'presidentOf', 'vicePresidentOf', 'minOfForeignAffairsOf', 'congressMemberOf'
    ]
    for field in all_role_fields:
        if infos.get(field):
            roles.append(role_display_name(field))
    for key in infos:
        if key.startswith('minOf') and key not in all_role_fields and infos[key]:
            roles.append(role_display_name(key))
    return ', '.join(roles) if roles else 'No government role'
