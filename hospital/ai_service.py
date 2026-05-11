import logging
import time
from datetime import date

from django.conf import settings
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

MODEL    = "lightning-ai/deepseek-v4-pro"
BASE_URL = "https://lightning.ai/api/v1/"
log      = logging.getLogger("hospital")


# ── DSPy module (Phase 6.5) ────────────────────────────────────────────────


def _dspy_module():
    """Lazy-built DSPy ChainOfThought signature for diagnosis. Cached per process.
    Returns None if dspy isn't importable or the LLM isn't configured."""
    if not getattr(settings, 'LIGHTNING_API_KEY', None):
        return None
    try:
        import dspy
    except ImportError:
        return None

    cache = getattr(_dspy_module, '_cache', None)
    if cache is not None:
        return cache

    try:
        # OpenAI-compatible endpoint
        lm = dspy.LM(
            f'openai/{MODEL}',
            api_key=settings.LIGHTNING_API_KEY,
            api_base=BASE_URL,
            max_tokens=200,
            temperature=0.3,
            cache=False,
        )
        dspy.configure(lm=lm)
    except Exception as exc:
        log.warning('DSPy init failed: %s', exc)
        return None

    class DiagnoseSig(dspy.Signature):
        """Suggest the most likely diagnosis for a clinic appointment, with a
        calibrated confidence score. Return ONLY the structured fields."""
        patient_age   = dspy.InputField(desc='Patient age in years')
        doctor_specialty = dspy.InputField(desc="Doctor's specialty")
        reason        = dspy.InputField(desc='Patient-reported reason for visit')
        diagnosis     = dspy.OutputField(desc='Single most-likely diagnosis (≤ 8 words)')
        confidence    = dspy.OutputField(desc='Confidence as a float between 0.0 and 1.0')
        rationale     = dspy.OutputField(desc='Two-sentence clinical reasoning')

    module = dspy.ChainOfThought(DiagnoseSig)
    _dspy_module._cache = module
    return module


def _build_prompt(appointment) -> str:
    try:
        age = (date.today() - appointment.patient.date_of_birth).days // 365
    except Exception:
        age = "unknown"

    return (
        f"You are a medical AI assistant. Based on the following appointment details, "
        f"provide a brief possible diagnosis and a confidence score between 0.0 and 1.0.\n\n"
        f"Patient age: {age}\n"
        f"Doctor specialty: {appointment.doctor.specialty}\n"
        f"Appointment reason: {appointment.reason}\n\n"
        f"Respond in this exact format (two lines only):\n"
        f"Diagnosis: <your diagnosis here>\n"
        f"Confidence: <number between 0.0 and 1.0>"
    )


def _parse_response(text: str) -> tuple[str, float]:
    diagnosis = ""
    confidence = 0.0
    for line in text.strip().splitlines():
        if line.lower().startswith("diagnosis:"):
            diagnosis = line.split(":", 1)[1].strip()
        elif line.lower().startswith("confidence:"):
            try:
                confidence = float(line.split(":", 1)[1].strip())
                confidence = max(0.0, min(1.0, confidence))
            except ValueError:
                pass
    return diagnosis or text.strip(), confidence


def _patient_age(appointment) -> int | str:
    try:
        return (date.today() - appointment.patient.date_of_birth).days // 365
    except Exception:
        return "unknown"


def _predict_with_dspy(appointment) -> dict | None:
    """Structured diagnosis via DSPy ChainOfThought. Returns the result dict or
    None to signal fallback to the legacy LangChain path."""
    module = _dspy_module()
    if module is None:
        return None
    start = time.time()
    try:
        out = module(
            patient_age=str(_patient_age(appointment)),
            doctor_specialty=appointment.doctor.specialty,
            reason=appointment.reason,
        )
        try:
            confidence = float(str(out.confidence).strip())
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        diagnosis = (str(out.diagnosis or '').strip()
                     or str(getattr(out, 'rationale', '') or '').strip())
        return {
            "predicted_diagnosis": diagnosis,
            "confidence_score":   confidence,
            "model_version":      f'{MODEL}+dspy.cot',
            "status":             "SUCCESS",
            "error_message":      "",
            "latency_ms":         int((time.time() - start) * 1000),
            "total_tokens":       0,  # dspy doesn't surface this uniformly
        }
    except Exception as exc:
        log.warning('DSPy diagnosis path failed, falling back: %s', exc)
        return None


def refine_symptoms(raw_text: str, specialty: str = '') -> dict:
    """Patient-facing AI Complete: takes a rough symptom description and rewrites
    it as a concise, clinically-useful "reason for visit". Never invents new
    symptoms — only structures and clarifies what the patient wrote.

    Returns:
        {"status": "SUCCESS"|"FAILED", "refined": str, "error_message": str}
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return {"status": "FAILED", "refined": "", "error_message": "Please type a few words first."}

    if not getattr(settings, 'LIGHTNING_API_KEY', None):
        return {"status": "FAILED", "refined": "",
                "error_message": "AI helper not configured (LIGHTNING_API_KEY missing)."}

    prompt = (
        "You are a clinical intake assistant helping a patient describe a chief "
        "complaint for their upcoming appointment. Rewrite the patient's note "
        "into ONE concise paragraph (max 3 sentences, ≤ 60 words). Keep their "
        "facts intact; do NOT invent new symptoms, durations, or severity. "
        "Use plain language. End without diagnostic guesses.\n\n"
        f"Doctor specialty (context only): {specialty or 'general practice'}\n"
        f"Patient's rough note:\n\"\"\"{raw_text}\"\"\"\n\n"
        "Return ONLY the rewritten paragraph — no preamble, no bullets."
    )

    llm = ChatOpenAI(
        model=MODEL,
        api_key=settings.LIGHTNING_API_KEY,
        base_url=BASE_URL,
        temperature=0.2,
        max_tokens=180,
        timeout=15.0,
    )
    try:
        resp = llm.invoke([HumanMessage(content=prompt)])
        refined = (resp.content or "").strip().strip('"').strip()
        if not refined:
            return {"status": "FAILED", "refined": "", "error_message": "Empty AI response."}
        return {"status": "SUCCESS", "refined": refined, "error_message": ""}
    except Exception as exc:
        log.warning('refine_symptoms failed: %s', exc)
        return {"status": "FAILED", "refined": "", "error_message": str(exc)}


def predict_diagnosis(appointment) -> dict:
    if not getattr(settings, 'LIGHTNING_API_KEY', None):
        return {
            "predicted_diagnosis": "",
            "confidence_score": 0.0,
            "model_version": MODEL,
            "status": "FAILED",
            "error_message": "LLM not configured (LIGHTNING_API_KEY missing).",
            "latency_ms": 0,
            "total_tokens": 0,
        }

    # Prefer the DSPy ChainOfThought path for structured output
    dspy_result = _predict_with_dspy(appointment)
    if dspy_result is not None:
        return dspy_result

    # Fallback: original LangChain prompt + line parser
    llm = ChatOpenAI(
        model=MODEL,
        api_key=settings.LIGHTNING_API_KEY,
        base_url=BASE_URL,
        temperature=0.3,
        max_tokens=150,
        timeout=20.0,
    )

    start = time.time()
    try:
        response = llm.invoke([HumanMessage(content=_build_prompt(appointment))])
        latency_ms = int((time.time() - start) * 1000)

        diagnosis, confidence = _parse_response(response.content)
        total_tokens = (response.usage_metadata or {}).get("total_tokens", 0) if hasattr(response, "usage_metadata") else 0

        return {
            "predicted_diagnosis": diagnosis,
            "confidence_score": confidence,
            "model_version": MODEL,
            "status": "SUCCESS",
            "error_message": "",
            "latency_ms": latency_ms,
            "total_tokens": total_tokens,
        }

    except Exception as exc:
        latency_ms = int((time.time() - start) * 1000)
        return {
            "predicted_diagnosis": "",
            "confidence_score": 0.0,
            "model_version": MODEL,
            "status": "FAILED",
            "error_message": str(exc),
            "latency_ms": latency_ms,
            "total_tokens": 0,
        }
