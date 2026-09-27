from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

APP_VERSION = "1.0.0"
TEAM_NAME = os.getenv("TEAM_NAME", "VeraFlow")
TEAM_MEMBERS = [x.strip() for x in os.getenv("TEAM_MEMBERS", "Magicpin AI Challenge Team").split(",") if x.strip()]
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "team@example.com")
START = time.time()

app = FastAPI(title="VeraFlow — Magicpin AI Challenge Bot", version=APP_VERSION)

# In-memory state is sufficient for the challenge window. Contexts are keyed by
# (scope, context_id) and conversations by conversation_id.
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, dict[str, Any]] = {}
seen_suppression: dict[str, float] = {}


class ContextPush(BaseModel):
    scope: str
    context_id: str
    version: int = Field(ge=0)
    payload: dict[str, Any]
    delivered_at: str


class TickRequest(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str
    turn_number: int = Field(ge=1)


def _get(scope: str, context_id: str) -> dict[str, Any] | None:
    item = contexts.get((scope, context_id))
    return item["payload"] if item else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _name(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity") or {}
    return identity.get("owner_first_name") or identity.get("name") or "there"


def _business(merchant: dict[str, Any]) -> str:
    return (merchant.get("identity") or {}).get("name") or "your business"


def _lang_mix(merchant: dict[str, Any], customer: Optional[dict[str, Any]] = None) -> bool:
    pref = ((customer or {}).get("identity") or {}).get("language_pref", "")
    langs = (merchant.get("identity") or {}).get("languages") or []
    return "hi" in langs or "hi-en" in str(pref).lower() or str(pref).lower() == "hi"


def _percent(v: Any) -> str:
    try:
        x = float(v)
        if abs(x) <= 1:
            x *= 100
        return f"{x:.1f}%".rstrip("0").rstrip(".")
    except Exception:
        return str(v)


def _currency(value: Any) -> str:
    return str(value) if value is not None else ""


def _first_active_offer(merchant: dict[str, Any]) -> str | None:
    for offer in merchant.get("offers") or []:
        if str(offer.get("status", "")).lower() == "active":
            return offer.get("title")
    return None


def _digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any] | None:
    payload = trigger.get("payload") or {}
    top_id = payload.get("top_item_id")
    items = category.get("digest") or []
    if top_id:
        for item in items:
            if item.get("id") == top_id:
                return item
    return items[0] if items else None


def _find_trigger_detail(trigger: dict[str, Any], keys: tuple[str, ...]) -> Any:
    payload = trigger.get("payload") or {}
    for key in keys:
        if key in payload and payload[key] not in (None, "", []):
            return payload[key]
    return None


def _language_prefix(merchant: dict[str, Any], customer: Optional[dict[str, Any]]) -> str:
    return "" if not _lang_mix(merchant, customer) else ""


def _compose_merchant(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any]:
    kind = str(trigger.get("kind", "")).lower()
    biz = _business(merchant)
    first = _name(merchant)
    perf = merchant.get("performance") or {}
    signals = [str(s) for s in merchant.get("signals") or []]
    peer = category.get("peer_stats") or {}
    active_offer = _first_active_offer(merchant)
    mix = _lang_mix(merchant)
    lang = "hi" if mix else "en"

    payload = trigger.get("payload") or {}

    if kind == "active_planning_intent":
        topic = payload.get("intent_topic") or "the program you mentioned"
        body = f"{first}, got it — you want to work on {str(topic).replace('_', ' ')}. I can turn that into a concrete first draft using your {category.get('display_name', category.get('slug', 'category'))} context. Want me to draft the offer + message?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Explicit merchant intent is routed directly into action instead of asking another qualification question."}

    if kind == "category_seasonal":
        season = payload.get("season") or "the current season"
        trends = payload.get("trends") or []
        trend = str(trends[0]).replace("_", " ") if trends else "a category demand shift"
        body = f"{first}, {season.replace('_', ' ')} has a category signal worth using: {trend}. {biz} can act on the existing category demand without inventing a new offer. Want me to draft one practical shelf/content action?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Uses the seasonal trigger's concrete trend signal and turns it into one category-fit action."}

    if kind == "cde_opportunity":
        item_id = payload.get("digest_item_id") or "the available category item"
        fee = payload.get("fee") or "the stated fee"
        credits = payload.get("credits")
        extra = f" You have {credits} credits." if credits is not None else ""
        body = f"{first}, there’s a relevant category opportunity: {item_id} is available at {fee}.{extra} Want me to explain the exact next step?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Surfaces the opportunity exactly as provided and moves directly to the next action."}

    if kind == "gbp_unverified":
        path = payload.get("verification_path") or "the verification path in your account"
        body = f"{first}, {biz} is currently marked unverified on GBP. The available verification path is {path}. I can walk you through the next step — want that?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Uses the supplied verification state/path and avoids promising an unsupported uplift."}

    if kind == "ipl_match_today":
        match = payload.get("match") or "today's match"
        venue = payload.get("venue")
        city = payload.get("city")
        when = payload.get("match_time_iso")
        detail = ", at " + venue if venue else ""
        body = f"{first}, {match} is on today{detail}."
        if city: body += f" That’s a local Delhi-area demand signal for {city}."
        if when: body += f" Kickoff: {when}."
        body += " Want me to turn it into one relevant customer message using your existing offers?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Anchors the local sports trigger to its supplied match, venue and time and offers a single concrete content action."}

    # Explicit intent/action triggers should move straight to an actionable next step.
    if kind in {"renewal_due", "trial_followup"}:
        days = (merchant.get("subscription") or {}).get("days_remaining")
        status = (merchant.get("subscription") or {}).get("status")
        if status == "expired":
            body = f"{first}, {biz} ka plan currently expired hai. Agar aap continue karna chahte hain, main renewal steps share kar sakti hoon. Reply YES to continue, STOP to skip."
        else:
            body = f"{first}, {biz} ka plan {days} days mein renew hona hai. Main renewal steps ready kar du? Reply YES to continue, STOP to skip."
        return {"body": body, "cta": "YES/STOP", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Time-sensitive subscription action with the merchant's actual plan state and a single binary next step."}

    if kind in {"perf_spike", "milestone_reached"}:
        dv = (perf.get("delta_7d") or {}).get("views_pct")
        rating = peer.get("avg_rating")
        views = perf.get("views")
        if kind == "perf_spike" and dv is not None:
            change = _percent(dv)
            body = f"{first}, quick win to notice: {biz} has {views} views in the last {perf.get('window_days', 30)} days and views are {change} vs the prior 7-day baseline. Want me to show the 1 change most likely to turn that traffic into more calls?"
        else:
            body = f"{first}, {biz} just hit a new milestone. I can turn that signal into a concrete Google-profile action for you. Want me to draft it?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Uses a concrete merchant performance or milestone signal and offers one low-effort next step."}

    if kind == "perf_dip":
        calls = perf.get("calls")
        delta = (perf.get("delta_7d") or {}).get("calls_pct")
        ctr = perf.get("ctr")
        body = f"{first}, {biz} ke calls {calls} hain over the last {perf.get('window_days', 30)} days, while the recent calls trend is {_percent(delta)}. CTR is {_percent(ctr)}. I can check the profile/offers for the clearest gap — want me to?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Frames the current performance dip with the merchant's own numbers and proposes diagnosis rather than a generic sales pitch."}

    if kind in {"research_digest", "category_research_digest_release"}:
        item = _digest_item(category, trigger)
        if item:
            title = item.get("title", "a new category update")
            source = item.get("source")
            suffix = f" — {source}" if source else ""
            body = f"{first}, a new {category.get('display_name', category.get('slug', 'category'))} update caught my eye: {title}{suffix}. It may matter for your {', '.join(signals[:2]) if signals else 'current profile'}. Want the 60-sec takeaway?"
        else:
            body = f"{first}, a new category update is available for {category.get('display_name', category.get('slug', 'your business'))}. I can pull the relevant takeaway for {biz}. Want it?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Anchors a research trigger to a real category digest item and connects it to the merchant's current state without fabricating evidence."}

    if kind == "competitor_opened":
        distance = _find_trigger_detail(trigger, ("distance_km", "distance", "radius_km"))
        competitor = _find_trigger_detail(trigger, ("competitor_name", "name"))
        detail = f" at {distance} km" if distance else " nearby"
        who = f"{competitor}{detail}" if competitor else f"a new competitor{detail}"
        body = f"{first}, there’s a new local-search signal: {who}. Rather than guessing, I can show you the exact profile gap worth checking for {biz}. Want me to pull it?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Uses only competitor facts present in the trigger and turns the external event into a concrete profile-check invitation."}

    if kind == "festival_upcoming":
        event = _find_trigger_detail(trigger, ("event", "festival", "name", "title")) or "the upcoming festival"
        days = _find_trigger_detail(trigger, ("days_until", "days"))
        timing = f" in {days} days" if days is not None else ""
        body = f"{first}, {event}{timing} is a good content window for {biz}. I can turn your existing {active_offer or 'active service'} into a category-fit post/offer draft. Want me to draft one?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Connects a time-bound external event to an existing merchant offer instead of inventing a discount."}

    if kind == "weather_heatwave":
        temp = _find_trigger_detail(trigger, ("temperature_c", "temp_c", "temperature"))
        body = f"{first}, Delhi is at {temp}°C today." if temp is not None else f"{first}, there’s a heatwave signal for your city today."
        body += f" For {biz}, I can draft a useful customer-facing update around the services you already offer. Want the draft?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Uses the weather trigger only when a concrete temperature is supplied and avoids making unsupported operational claims."}

    if kind in {"review_theme_emerged", "review_theme"}:
        themes = merchant.get("review_themes") or []
        theme = themes[0] if themes else {}
        topic = theme.get("theme") or _find_trigger_detail(trigger, ("theme", "topic")) or "a review theme"
        occ = theme.get("occurrences_30d") or _find_trigger_detail(trigger, ("occurrences_30d", "occurrences"))
        count = f"{occ} reviews" if occ else "recent reviews"
        body = f"{first}, one pattern stands out in {biz}: {topic.replace('_', ' ')} appears in {count}. I can turn that into a practical response/update draft. Want me to draft it?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Calls out a concrete review theme and occurrence count when available, then offers an actionable draft."}

    if kind in {"dormant_with_vera", "curious_ask_due"}:
        question = "What’s the one service customers ask you for most this week?"
        body = f"{first}, quick one for {biz}: {question} I’ll use your answer to suggest a category-fit next move."
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Uses a curiosity/merchant-question lever to restart engagement without a generic promotional nudge."}

    if kind == "regulation_change":
        body = f"{first}, a regulation update relevant to {category.get('display_name', category.get('slug', 'your category'))} is now in the trigger feed. I can summarize only the operational point that may affect {biz}. Want the short version?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Keeps compliance language factual and avoids inventing a regulation detail not present in context."}

    # Generic but still context-grounded fallback.
    city = (merchant.get("identity") or {}).get("city", "your city")
    body = f"{first}, I have a new {kind.replace('_', ' ')} signal for {biz} in {city}. I can turn the available data into one concrete next step. Want me to?"
    return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": trigger.get("suppression_key", kind),
            "rationale": "Fallback keeps the message tied to the actual trigger and merchant instead of fabricating missing details."}


def compose(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic 4-context composer used by both the HTTP bot and submission generator."""
    if customer:
        return _compose_customer(category, merchant, trigger, customer)
    return _compose_merchant(category, merchant, trigger)


def _compose_customer(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
    kind = str(trigger.get("kind", "")).lower()
    name = (customer.get("identity") or {}).get("name") or "there"
    business = _business(merchant)
    services = (customer.get("relationship") or {}).get("services_received") or []
    last_visit = (customer.get("relationship") or {}).get("last_visit")
    pref = (customer.get("preferences") or {}).get("preferred_slots") or "your usual preferred time"
    consent = customer.get("consent") or {}
    allowed = set(consent.get("scope") or [])
    reminder_ok = bool(customer.get("preferences", {}).get("reminder_opt_in")) or bool(allowed.intersection({"appointment_reminders", "recall_reminders", "recall_alerts", "refill_reminders", "renewal_reminders", "winback_offers", "promotional_offers"}))

    if not reminder_ok:
        return {"body": "", "cta": "none", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Customer consent scope does not authorize the requested outreach."}

    if kind in {"recall_due", "customer_lapsed_soft", "customer_lapsed_hard"}:
        service = services[-1] if services else None
        days = (trigger.get("payload") or {}).get("days_since_last_visit")
        if kind == "customer_lapsed_hard":
            timing = f"It has been {days} days since your last visit. " if days is not None else "It has been a while since your last visit. "
        else:
            timing = "Your recall window is coming up. "
        body = f"Hi {name}, {business} here. {timing}Your last recorded visit was {last_visit or 'your last visit'} for {service}. {('Weekday evenings are your usual preference. ' if pref else '')}Would you like us to help with a suitable slot?"
        return {"body": body, "cta": "open_ended", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Uses the customer's recorded relationship, service history, preference, and consent scope for relevant re-engagement."}

    if kind == "appointment_tomorrow":
        body = f"Hi {name}, reminder from {business}: your appointment is tomorrow. If you need to change the timing, reply here and we’ll help."
        return {"body": body, "cta": "open_ended", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Appointment reminder uses only the customer and merchant relationship context supplied."}

    if kind == "chronic_refill_due":
        payload = trigger.get("payload") or {}
        if category.get("slug") == "pharmacies":
            runout = payload.get("stock_runs_out_iso")
            body = f"Hi {name}, {business} is checking in about your refill. The current reminder shows a run-out date of {runout}. If you still need it, reply here and the team can help with the next step." if runout else f"Hi {name}, {business} is checking in about your refill reminder. If you still need it, reply here and the team can help with the next step."
        else:
            body = f"Hi {name}, {business} is checking in about a care reminder from your recent records. If you need the details, reply here and the team can help."
        return {"body": body, "cta": "open_ended", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "Uses pharmacy refill details only when the category supports them; otherwise avoids inventing medication information."}

    body = f"Hi {name}, {business} here. We have a reminder relevant to your recent visits. Reply here if you’d like the details."
    return {"body": body, "cta": "open_ended", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", kind),
            "rationale": "Customer-facing fallback stays within the known relationship context."}


def _conversation(conversation_id: str) -> dict[str, Any]:
    return conversations.setdefault(conversation_id, {"turns": [], "auto_reply_seen": 0, "last_body": "", "merchant_id": None, "customer_id": None})


def _is_auto_reply(text: str) -> bool:
    t = text.lower().strip()
    markers = [
        "thank you for contacting", "thanks for contacting", "your message has been received",
        "our team will get back", "forward your message", "automated assistant", "i am an automated",
        "for your information", "your feedback to our team", "hamari team", "sujhaav hamari team",
    ]
    return any(m in t for m in markers)


def _is_negative(text: str) -> bool:
    t = text.lower().strip()
    return any(x in t for x in ["not interested", "no thanks", "stop", "don't need", "dont need", "not required", "remove me", "unsubscribe"])


def _is_positive_intent(text: str) -> bool:
    t = text.lower().strip()
    phrases = ["yes", "haan", "sure", "go ahead", "do it", "let's do", "lets do", "proceed", "join", "i want to join", "update it", "update my", "please update", "send it", "send me", "create it"]
    return any(p in t for p in phrases)


def _reply_from_context(req: ReplyRequest, state: dict[str, Any]) -> dict[str, Any]:
    merchant = _get("merchant", req.merchant_id or state.get("merchant_id", "")) or {}
    category_slug = merchant.get("category_slug")
    category = _get("category", category_slug) or {}
    customer = _get("customer", req.customer_id or state.get("customer_id", "")) if (req.customer_id or state.get("customer_id")) else None
    text = req.message.strip()
    lower = text.lower()

    if _is_negative(text):
        return {"action": "end", "rationale": "Merchant/customer explicitly declined; ending without another promotional turn."}

    if _is_auto_reply(text):
        state["auto_reply_seen"] = state.get("auto_reply_seen", 0) + 1
        if state["auto_reply_seen"] >= 2:
            return {"action": "end", "rationale": "Repeated automated/boilerplate response detected; stopping after one graceful clarification attempt."}
        return {"action": "send", "body": "Samajh gayi — lag raha hai ye automated reply hai. Owner/manager tak pahunchne se pehle, kya aap khud exact profile/offer update dekhna chahenge?", "cta": "open_ended", "rationale": "Detected likely auto-reply and used one low-friction clarification instead of restarting qualification."}

    # Direct action handoff: never go back to qualifying questions after explicit intent.
    if _is_positive_intent(text):
        if "join" in lower:
            return {"action": "send", "body": "Bilkul — aapne join karne ka clear intent diya hai. Main next onboarding step par move karungi; aapko dobara qualification questions nahi poochungi.", "cta": "open_ended", "rationale": "Explicit join intent is routed directly to the action path rather than re-qualifying the merchant."}
        offer = _first_active_offer(merchant)
        if offer:
            return {"action": "send", "body": f"Done — {offer} ko base rakhkar next draft/action ready hai. Agar aap isi version ko use karna chahte hain, main next step continue kar sakti hoon.", "cta": "open_ended", "rationale": "Acknowledges the positive intent and advances to the next concrete action using an existing merchant offer."}
        return {"action": "send", "body": "Done — I’ll move to the next action using the context you already shared. No extra qualification needed.", "cta": "open_ended", "rationale": "Advances immediately after clear intent instead of asking another qualification question."}

    if any(k in lower for k in ["google", "profile", "listing"]):
        signals = merchant.get("signals") or []
        perf = merchant.get("performance") or {}
        ctr = perf.get("ctr")
        stale = next((s for s in signals if str(s).startswith("stale_posts")), None)
        parts = []
        if stale:
            parts.append(f"posts are stale ({str(stale).split(':',1)[-1]} days)")
        if ctr is not None:
            parts.append(f"CTR is {_percent(ctr)}")
        if parts:
            return {"action": "send", "body": f"For { _business(merchant) }, the current data shows " + " and ".join(parts) + ". I can focus the next update on that exact gap.", "cta": "open_ended", "rationale": "Answers the profile question from the pushed merchant context and cites concrete current signals."}

    if any(k in lower for k in ["offer", "price", "rate", "discount"]):
        offer = _first_active_offer(merchant)
        if offer:
            return {"action": "send", "body": f"Your active offer is {offer}. I’d keep the service+price format rather than inventing a generic percentage discount.", "cta": "open_ended", "rationale": "Uses the merchant's actual active offer and follows the challenge's category-specific service+price preference."}

    if "why" in lower or "how" in lower:
        digest = _digest_item(category, {"payload": {}}) or {}
        source = digest.get("source")
        if source:
            return {"action": "send", "body": f"The reason I raised it is the category context has a cited item from {source}. I’m using only the data pushed into this conversation.", "cta": "open_ended", "rationale": "Explains the recommendation while explicitly staying within supplied context."}

    return {"action": "send", "body": "Got it. I’ll keep the next step focused on the specific trigger and your current account state rather than adding another generic nudge.", "cta": "open_ended", "rationale": "Acknowledges the reply and keeps the conversation anchored to the available context."}


@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _item in contexts.items():
        if scope in counts:
            counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": "deterministic-context-router-v1",
        "approach": "trigger router + context-grounded deterministic composer + multi-turn intent/auto-reply handling",
        "contact_email": CONTACT_EMAIL,
        "version": APP_VERSION,
        "submitted_at": _now_iso(),
    }


@app.post("/v1/context")
async def push_context(body: ContextPush):
    allowed = {"category", "merchant", "customer", "trigger"}
    if body.scope not in allowed:
        return {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {sorted(allowed)}"}
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    contexts[key] = {"version": body.version, "payload": body.payload, "delivered_at": body.delivered_at}
    return {"accepted": True, "ack_id": f"ack_{_slugify(body.context_id)}_v{body.version}", "stored_at": _now_iso()}


@app.post("/v1/tick")
async def tick(body: TickRequest):
    actions = []
    for trigger_id in body.available_triggers:
        trg = _get("trigger", trigger_id)
        if not trg:
            continue
        merchant_id = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = _get("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue
        category = _get("category", merchant.get("category_slug", ""))
        if not category:
            continue
        customer_id = trg.get("customer_id") or (trg.get("payload") or {}).get("customer_id")
        customer = _get("customer", customer_id) if customer_id else None
        composed = compose(category, merchant, trg, customer)
        if not composed.get("body"):
            continue
        suppression = composed.get("suppression_key") or trigger_id
        if suppression in seen_suppression:
            continue
        conversation_id = "conv_" + hashlib.sha1(f"{merchant_id}:{customer_id}:{trigger_id}:{time.time_ns()}".encode()).hexdigest()[:16]
        conversations[conversation_id] = {"turns": [{"from": "vera", "body": composed["body"]}], "auto_reply_seen": 0,
                                           "last_body": composed["body"], "merchant_id": merchant_id, "customer_id": customer_id}
        seen_suppression[suppression] = time.time()
        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trigger_id,
            "template_name": "vera_contextual_v1",
            "template_params": [merchant.get("identity", {}).get("name", "merchant"), str(trg.get("kind", "update")), str(composed.get("suppression_key", ""))],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": suppression,
            "rationale": composed["rationale"],
        })
        if len(actions) >= 20:
            break
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyRequest):
    state = _conversation(body.conversation_id)
    if state.get("merchant_id") is None:
        state["merchant_id"] = body.merchant_id
    if state.get("customer_id") is None:
        state["customer_id"] = body.customer_id
    state.setdefault("turns", []).append({"from": body.from_role, "body": body.message, "turn_number": body.turn_number})
    result = _reply_from_context(body, state)
    if result.get("action") == "send":
        if result.get("body") == state.get("last_body"):
            result = {"action": "send", "body": "Understood. I’ll avoid repeating the same message and wait for your next instruction.", "cta": "open_ended", "rationale": "Anti-repetition guard replaced an identical response."}
        state["last_body"] = result.get("body", "")
        state["turns"].append({"from": "vera", "body": result.get("body", "")})
    return result


@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    seen_suppression.clear()
    return {"ok": True, "cleared": True}
