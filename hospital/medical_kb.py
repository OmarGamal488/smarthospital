"""Medical knowledge integrations for the chatbot.

Three layers, each gracefully degrades if its endpoints/keys are missing:

1. **OpenFDA** (public, no key) — drug labels, adverse reactions.
2. **MedlinePlus Connect** (public, no key) — patient-friendly disease/condition pages.
3. **NIH Clinical Tables** (public, no key) — autocomplete for ICD-10 codes & condition names.
4. **Infermedica** (env keys: `INFERMEDICA_APP_ID`, `INFERMEDICA_APP_KEY`) — symptom triage / differential.
5. **Lightning embeddings RAG** (env: `LIGHTNING_EMBED_MODEL`) — local curated KB retrieved by cosine similarity.

Public, free APIs and the local curated KB ship enabled. Infermedica & RAG turn on
only when their env vars are present.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import httpx
from django.conf import settings


# ── Curated 50-entry medical KB shipped in repo ──────────────────────────────

_KB_PATH = Path(__file__).resolve().parent / 'medical_kb.json'
_KB_CACHE: list[dict[str, Any]] | None = None


def _load_kb() -> list[dict[str, Any]]:
    global _KB_CACHE
    if _KB_CACHE is not None:
        return _KB_CACHE
    if not _KB_PATH.exists():
        _KB_CACHE = []
        return _KB_CACHE
    try:
        with _KB_PATH.open('r', encoding='utf-8') as f:
            _KB_CACHE = json.load(f) or []
    except (OSError, json.JSONDecodeError):
        _KB_CACHE = []
    return _KB_CACHE


def _bow(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for tok in text.lower().split():
        tok = ''.join(c for c in tok if c.isalnum())
        if not tok or len(tok) < 3:
            continue
        out[tok] = out.get(tok, 0) + 1
    return out


def _cosine(a: dict[str, int], b: dict[str, int]) -> float:
    inter = set(a) & set(b)
    if not inter:
        return 0.0
    dot = sum(a[k] * b[k] for k in inter)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def kb_search(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Lexical-only retrieval over the curated KB.

    Acts as a graceful fallback when a Lightning embeddings endpoint is not
    configured; remote embeddings (when available) are used by `kb_search_remote`.
    """
    entries = _load_kb()
    if not entries:
        return []
    qbow = _bow(query)
    scored = []
    for e in entries:
        haystack = ' '.join([
            e.get('name', ''), e.get('summary', ''), e.get('symptoms', ''),
            ' '.join(e.get('keywords', [])),
        ])
        score = _cosine(qbow, _bow(haystack))
        if score > 0:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:top_k]]


# ── Public APIs (no key required) ───────────────────────────────────────────


_DEFAULT_TIMEOUT = httpx.Timeout(8.0, connect=5.0)


def openfda_drug_label(name: str) -> dict[str, Any] | None:
    """Look up a drug by brand or generic name on OpenFDA. Returns a digest dict
    or None on missing/error."""
    name = (name or '').strip()
    if not name:
        return None
    try:
        params = {
            'search': f'(openfda.brand_name:"{name}" OR openfda.generic_name:"{name}")',
            'limit':  1,
        }
        r = httpx.get('https://api.fda.gov/drug/label.json',
                      params=params, timeout=_DEFAULT_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        results = data.get('results') or []
        if not results:
            return None
        rec = results[0]
        ofda = rec.get('openfda') or {}
        return {
            'brand_name':         (ofda.get('brand_name') or [name])[0],
            'generic_name':       (ofda.get('generic_name') or [''])[0],
            'manufacturer':       (ofda.get('manufacturer_name') or [''])[0],
            'indications':        (rec.get('indications_and_usage') or [''])[0][:500],
            'warnings':           (rec.get('warnings') or [''])[0][:400],
            'adverse_reactions':  (rec.get('adverse_reactions') or [''])[0][:400],
            'dosage':             (rec.get('dosage_and_administration') or [''])[0][:300],
        }
    except Exception:
        return None


def medlineplus_explain(condition: str) -> dict[str, Any] | None:
    """Patient-friendly summary for a condition via MedlinePlus Connect.
    Uses the ICD-10 lookup as the bridge."""
    condition = (condition or '').strip()
    if not condition:
        return None
    icd_hits = clinicaltables_search(condition, table='icd10cm', max_list=1)
    if not icd_hits:
        return None
    code = icd_hits[0]['code']
    try:
        params = {
            'mainSearchCriteria.v.cs': '2.16.840.1.113883.6.90',  # ICD-10-CM OID
            'mainSearchCriteria.v.c':  code,
            'knowledgeResponseType':   'application/json',
        }
        r = httpx.get('https://connect.medlineplus.gov/service',
                      params=params, timeout=_DEFAULT_TIMEOUT)
        if r.status_code != 200:
            return None
        feed = r.json().get('feed') or {}
        entries = feed.get('entry') or []
        if not entries:
            return None
        entry = entries[0]
        return {
            'icd10':   code,
            'title':   entry.get('title', {}).get('_value', condition),
            'summary': entry.get('summary', {}).get('_value', '')[:600],
            'link':    (entry.get('link') or [{}])[0].get('href', ''),
        }
    except Exception:
        return None


def clinicaltables_search(query: str, table: str = 'icd10cm', max_list: int = 5) -> list[dict[str, str]]:
    """NIH Clinical Tables autocomplete. table ∈ {'icd10cm', 'conditions', 'rxterms'}."""
    query = (query or '').strip()
    if not query:
        return []
    url = f'https://clinicaltables.nlm.nih.gov/api/{table}/v3/search'
    try:
        r = httpx.get(url, params={'terms': query, 'maxList': max_list},
                      timeout=_DEFAULT_TIMEOUT)
        if r.status_code != 200:
            return []
        # API returns: [total, codes, _, display_strings_2d]
        data = r.json()
        codes = data[1] or []
        rows  = data[3] or []
        results = []
        for i, code in enumerate(codes):
            display = rows[i] if i < len(rows) else []
            results.append({
                'code':    code,
                'display': display[0] if display else code,
            })
        return results
    except Exception:
        return []


# ── Infermedica (env-keyed) ─────────────────────────────────────────────────


def _infermedica_creds() -> tuple[str, str] | None:
    app_id  = os.environ.get('INFERMEDICA_APP_ID')
    app_key = os.environ.get('INFERMEDICA_APP_KEY')
    if app_id and app_key:
        return app_id, app_key
    return None


def infermedica_parse(text: str, age: int = 35, sex: str = 'male') -> dict[str, Any] | None:
    """Parse free-text symptoms into Infermedica concept IDs.
    Returns {mentions: [...], present: [{id, name}, ...]}, or None if not configured."""
    creds = _infermedica_creds()
    if not creds:
        return None
    app_id, app_key = creds
    try:
        r = httpx.post(
            'https://api.infermedica.com/v3/parse',
            json={'text': text, 'age': {'value': age}, 'sex': sex,
                  'include_tokens': False, 'concept_types': ['symptom']},
            headers={'App-Id': app_id, 'App-Key': app_key,
                     'Content-Type': 'application/json'},
            timeout=_DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        mentions = data.get('mentions') or []
        present = [{'id': m['id'], 'name': m.get('common_name') or m.get('name')}
                   for m in mentions if m.get('choice_id') == 'present']
        return {'mentions': mentions, 'present': present}
    except Exception:
        return None


def infermedica_diagnose(symptom_ids: list[str], age: int = 35, sex: str = 'male') -> dict[str, Any] | None:
    """Run Infermedica differential diagnosis on a list of symptom IDs."""
    creds = _infermedica_creds()
    if not creds or not symptom_ids:
        return None
    app_id, app_key = creds
    try:
        evidence = [{'id': sid, 'choice_id': 'present', 'source': 'initial'}
                    for sid in symptom_ids]
        r = httpx.post(
            'https://api.infermedica.com/v3/diagnosis',
            json={'sex': sex, 'age': {'value': age}, 'evidence': evidence},
            headers={'App-Id': app_id, 'App-Key': app_key,
                     'Content-Type': 'application/json'},
            timeout=_DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return {
            'should_stop':   data.get('should_stop'),
            'conditions':    [
                {'id': c['id'], 'name': c.get('common_name') or c.get('name'),
                 'probability': c.get('probability', 0.0)}
                for c in (data.get('conditions') or [])[:5]
            ],
            'question':      data.get('question'),  # follow-up suggestion
            'extras':        data.get('extras'),
        }
    except Exception:
        return None


def infermedica_triage(symptom_ids: list[str], age: int = 35, sex: str = 'male') -> dict[str, Any] | None:
    """Suggest urgency level (consultation_24, consultation, self_care, ...)."""
    creds = _infermedica_creds()
    if not creds or not symptom_ids:
        return None
    app_id, app_key = creds
    try:
        evidence = [{'id': sid, 'choice_id': 'present', 'source': 'initial'}
                    for sid in symptom_ids]
        r = httpx.post(
            'https://api.infermedica.com/v3/triage',
            json={'sex': sex, 'age': {'value': age}, 'evidence': evidence},
            headers={'App-Id': app_id, 'App-Key': app_key,
                     'Content-Type': 'application/json'},
            timeout=_DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return {
            'level':       data.get('triage_level'),
            'description': data.get('description'),
            'label':       data.get('label'),
        }
    except Exception:
        return None


# ── Lightning embeddings RAG (env-keyed) ────────────────────────────────────


def _lightning_embed_model() -> str | None:
    return os.environ.get('LIGHTNING_EMBED_MODEL')


def _embed_one(text: str) -> list[float] | None:
    """Single-shot embedding via the Lightning OpenAI-compatible endpoint."""
    model = _lightning_embed_model()
    if not model or not getattr(settings, 'LIGHTNING_API_KEY', None):
        return None
    try:
        r = httpx.post(
            'https://lightning.ai/api/v1/embeddings',
            headers={'Authorization': f'Bearer {settings.LIGHTNING_API_KEY}',
                     'Content-Type': 'application/json'},
            json={'model': model, 'input': text},
            timeout=_DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return data['data'][0]['embedding']
    except Exception:
        return None


def kb_search_remote(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Embedding-based KB retrieval. Falls back to lexical kb_search if the
    embedding endpoint isn't configured or fails."""
    entries = _load_kb()
    if not entries:
        return []
    qvec = _embed_one(query)
    if qvec is None:
        return kb_search(query, top_k=top_k)
    scored = []
    for e in entries:
        evec = e.get('_embedding')
        if not evec:
            continue
        # cosine on dense vectors
        dot = sum(a * b for a, b in zip(qvec, evec))
        nq = math.sqrt(sum(a * a for a in qvec))
        ne = math.sqrt(sum(a * a for a in evec))
        if not nq or not ne:
            continue
        scored.append((dot / (nq * ne), e))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return kb_search(query, top_k=top_k)
    return [e for _, e in scored[:top_k]]
