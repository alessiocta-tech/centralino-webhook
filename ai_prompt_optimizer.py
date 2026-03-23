#!/usr/bin/env python3
"""
AI Prompt Optimizer — Centralino Automatizzato DERIONE
=======================================================
Analizza ogni chiamata ricevuta dal webhook ElevenLabs,
trova pattern di miglioramento e propone aggiornamenti automatici
al prompt dell'agente "Giulia" su ElevenLabs.

FLUSSO:
  1. Webhook riceve trascrizione → chiama analyze_and_propose()
  2. Claude analizza la chiamata + storico recente
  3. Se trova miglioramenti → salva proposta + invia email ad Alessio
  4. Alessio clicca "Approva" → apply_approved_change() → PATCH ElevenLabs
  5. Il prompt si aggiorna in produzione automaticamente

VARIABILI D'AMBIENTE RICHIESTE (Railway):
  CLAUDE_API_KEY         = sk-ant-...
  ELEVENLABS_API_KEY     = 5c0c2095...
  ELEVENLABS_AGENT_ID    = agent_2701kgrsp2gzec6rraa6bfgtrwfw
  APPROVAL_BASE_URL      = https://centralino-webhook-production.up.railway.app
  NOTIFICATION_EMAIL     = alessiocta@gmail.com
  SMTP_HOST              = smtp.gmail.com  (o il tuo provider)
  SMTP_PORT              = 587
  SMTP_USER              = tua@email.com
  SMTP_PASSWORD          = app-password-gmail
"""

import hashlib
import hmac as hmac_lib
import json
import os
import secrets
import smtplib
import traceback
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
import requests

# ─────────────────────────────────────────────────────────────────
#  CONFIGURAZIONE
# ─────────────────────────────────────────────────────────────────
CLAUDE_API_KEY       = os.environ.get("CLAUDE_API_KEY", "")
ELEVENLABS_API_KEY   = os.environ.get("ELEVENLABS_API_KEY", "5c0c2095d6c7cf69a3b24034f691ccc22eb4a9c7c0b58ca70a2a7e46ddcd86f6")
ELEVENLABS_AGENT_ID  = os.environ.get("ELEVENLABS_AGENT_ID", "agent_2701kgrsp2gzec6rraa6bfgtrwfw")
APPROVAL_BASE_URL    = os.environ.get("APPROVAL_BASE_URL", "https://centralino-webhook-production.up.railway.app")
NOTIFICATION_EMAIL   = os.environ.get("NOTIFICATION_EMAIL", "alessiocta@gmail.com")
SMTP_HOST            = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT            = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER            = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD        = os.environ.get("SMTP_PASSWORD", "")

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"
CLAUDE_API_URL  = "https://api.anthropic.com/v1/messages"

# Storage locale (Railway ephemeral — sostituire con DB per produzione)
STORAGE_DIR      = Path(os.environ.get("STORAGE_DIR", "/tmp/centralino_calls"))
PROPOSALS_FILE   = STORAGE_DIR / "pending_proposals.json"
HISTORY_FILE     = STORAGE_DIR / "prompt_history.json"
LOG_FILE         = STORAGE_DIR / "calls_log.jsonl"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Quante chiamate analizzare insieme per trovare pattern
CALLS_FOR_PATTERN_ANALYSIS = 10

# Soglia minima di fiducia per proporre una modifica (0-100)
CONFIDENCE_THRESHOLD = 65


# ─────────────────────────────────────────────────────────────────
#  1. LEGGI PROMPT CORRENTE DA ELEVENLABS
# ─────────────────────────────────────────────────────────────────
def get_current_agent_config() -> dict:
    """Scarica la configurazione completa dell'agente da ElevenLabs."""
    url = f"{ELEVENLABS_BASE}/convai/agents/{ELEVENLABS_AGENT_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Errore ElevenLabs GET agent: {resp.status_code} - {resp.text}")
    return resp.json()


def get_current_prompt() -> str:
    """Estrae solo il testo del prompt di sistema dell'agente."""
    config = get_current_agent_config()
    return (
        config
        .get("conversation_config", {})
        .get("agent", {})
        .get("prompt", {})
        .get("prompt", "")
    )


# ─────────────────────────────────────────────────────────────────
#  2. ANALISI SINGOLA CHIAMATA
# ─────────────────────────────────────────────────────────────────
def analyze_single_call(transcript_record: dict, current_prompt: str) -> dict:
    """
    Usa Claude per analizzare una singola trascrizione.
    Restituisce un dizionario con problemi rilevati e suggerimenti.
    """
    if not CLAUDE_API_KEY:
        print("⚠️  CLAUDE_API_KEY non configurata — analisi AI saltata")
        return {"problems": [], "suggestions": [], "confidence": 0}

    transcript = transcript_record.get("transcript", [])
    analysis   = transcript_record.get("analysis", {})
    evaluation = analysis.get("evaluation_criteria_results", {})
    data_coll  = analysis.get("data_collection_results", {})

    transcript_text = "\n".join(
        f"[{t.get('role', '?').upper()}] {t.get('message', '')}"
        for t in transcript
    )

    system_msg = """Sei un esperto di conversational AI specializzato nell'ottimizzazione di agenti vocali per ristoranti italiani.
Analizza la trascrizione di una chiamata al centralino del ristorante DERIONE e identifica problemi nel comportamento dell'agente "Giulia".

Rispondi SEMPRE e SOLO con un JSON valido nel formato esatto specificato. Nessun testo prima o dopo il JSON."""

    user_msg = f"""TRASCRIZIONE CHIAMATA:
{transcript_text}

ESITO VALUTAZIONE ELEVENLABS:
{json.dumps(evaluation, ensure_ascii=False, indent=2)}

DATI RACCOLTI:
{json.dumps(data_coll, ensure_ascii=False, indent=2)}
ESTRATTO PROMPT CORRENTE (prime 2000 caratteri):
{current_prompt[:2000]}

Analizza questa chiamata e restituisci un JSON con questa struttura esatta:
{{
  "problems": [
    {{
      "tipo": "string",
      "descrizione": "string — cosa è andato storto in modo specifico",
      "turno_conversazione": 0,
      "gravita": "alta|media|bassa"
    }}
  ],
  "punti_positivi": ["string"],
  "suggerimenti_prompt": [
    {{
      "problema_correlato": "string",
      "modifica_suggerita": "string — testo ESATTO da aggiungere/modificare nel prompt",
      "posizione": "inizio|fine|sezione_prenotazioni|sezione_orari|sezione_menu|generale",
      "ragione": "string — perché questa modifica aiuterebbe"
    }}
  ],
  "confidence": 0,
  "richiede_modifica_prompt": false
}}"""

    try:
        resp = requests.post(
            CLAUDE_API_URL,
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-opus-4-6",
                "max_tokens": 2000,
                "system": system_msg,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"]
        result = json.loads(content)
        print(f"✅ Analisi completata — confidence: {result.get('confidence', 0)}%")
        return result
    except json.JSONDecodeError as e:
        print(f"⚠️  Risposta Claude non è JSON valido: {e}")
        return {"problems": [], "suggestions": [], "confidence": 0, "richiede_modifica_prompt": False}
    except Exception as e:
        print(f"❌ Errore analisi Claude: {e}")
        return {"problems": [], "suggestions": [], "confidence": 0, "richiede_modifica_prompt": False}


# ─────────────────────────────────────────────────────────────────
#  3. ANALISI PATTERN SU PIÙ CHIAMATE
# ─────────────────────────────────────────────────────────────────
def analyze_patterns_and_generate_proposal(recent_analyses: list, current_prompt: str) -> dict:
    """
    Dato un insieme di analisi recenti, genera una proposta di modifica
    al prompt consolidata (pattern ricorrenti hanno priorità più alta).
    """
    if not CLAUDE_API_KEY or not recent_analyses:
        return None

    problems_summary = []
    for a in recent_analyses:
        for p in a.get("problems", []):
            problems_summary.append(f"- [{p.get('gravita','?').upper()}] {p.get('tipo','?')}: {p.get('descrizione','')}")

    suggestions_summary = []
    for a in recent_analyses:
        for s in a.get("suggerimenti_prompt", []):
            suggestions_summary.append(f"- {s.get('modifica_suggerita','')} (posizione: {s.get('posizione','?')})")

    system_msg = """Sei un esperto di prompt engineering per agenti vocali conversazionali.
Devi generare una modifica precisa e mirata al prompt di un agente AI che gestisce prenotazioni telefoniche per il ristorante DERIONE (sede Appia, Roma).
Rispondi SOLO con JSON valido, nessun testo extra."""

    user_msg = f"""Analisi di {len(recent_analyses)} chiamate recenti hanno rilevato questi problemi ricorrenti:

PROBLEMI RILEVATI:
{chr(10).join(problems_summary[:30])}

SUGGERIMENTI DEGLI ANALIZZATORI:
{chr(10).join(suggestions_summary[:20])}

PROMPT CORRENTE COMPLETO:
{current_prompt}
Genera una proposta di modifica al prompt. Restituisci questo JSON:
{{
  "titolo": "string — titolo breve della modifica (max 60 caratteri)",
  "descrizione": "string — spiegazione chiara di cosa cambia e perché",
  "problemi_risolti": ["string"],
  "prompt_aggiornato": "string — IL PROMPT COMPLETO AGGIORNATO con le modifiche integrate",
  "diff_summary": "string — elenco puntato delle modifiche specifiche apportate",
  "confidence": 0,
  "impatto_stimato": "alto|medio|basso",
  "rischio": "alto|medio|basso",
  "note_per_revisione": "string — cosa il revisore umano dovrebbe controllare prima di approvare"
}}

REGOLE IMPORTANTI:
- Non cambiare la struttura o la personalità di Giulia
- Non rimuovere funzionalità esistenti, solo migliorare
- Mantieni lo stesso tono professionale e cordiale in italiano
- Il prompt aggiornato deve essere completo (non solo il diff)
- Se il rischio è alto, spiega dettagliatamente nelle note"""

    try:
        resp = requests.post(
            CLAUDE_API_URL,
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-opus-4-6",
                "max_tokens": 8000,
                "system": system_msg,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"]
        proposal = json.loads(content)
        print(f"💡 Proposta generata: '{proposal.get('titolo')}' (confidence: {proposal.get('confidence')}%)")
        return proposal
    except Exception as e:
        print(f"❌ Errore generazione proposta: {e}")
        traceback.print_exc()
        return None


# ─────────────────────────────────────────────────────────────────
#  4. GESTIONE PROPOSTE PENDENTI
# ─────────────────────────────────────────────────────────────────
def load_proposals() -> dict:
    if PROPOSALS_FILE.exists():
        try:
            return json.loads(PROPOSALS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_proposals(proposals: dict):
    PROPOSALS_FILE.write_text(json.dumps(proposals, ensure_ascii=False, indent=2))


def create_proposal(proposal_data: dict, conversation_ids: list) -> str:
    """Salva una proposta e restituisce il token univoco."""
    token = secrets.token_urlsafe(32)
    proposals = load_proposals()
    proposals[token] = {
        "token": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "proposal": proposal_data,
        "conversation_ids": conversation_ids,
    }
    save_proposals(proposals)
    print(f"📝 Proposta salvata con token: {token[:10]}...")
    return token


def get_proposal(token: str) -> Optional[dict]:
    proposals = load_proposals()
    return proposals.get(token)


def mark_proposal(token: str, status: str):
    proposals = load_proposals()
    if token in proposals:
        proposals[token]["status"] = status
        proposals[token]["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_proposals(proposals)


# ─────────────────────────────────────────────────────────────────
#  5. AGGIORNA PROMPT SU ELEVENLABS
# ─────────────────────────────────────────────────────────────────
def update_agent_prompt(new_prompt: str) -> bool:
    """
    Aggiorna il prompt dell'agente su ElevenLabs via PATCH API.
    Salva il vecchio prompt nella cronologia prima di sovrascrivere.
    """
    try:
        old_prompt = get_current_prompt()
        history = []
        if HISTORY_FILE.exists():
            history = json.loads(HISTORY_FILE.read_text())
        history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prompt": old_prompt,
        })
        HISTORY_FILE.write_text(json.dumps(history[-20:], ensure_ascii=False, indent=2))
        print(f"📚 Vecchio prompt salvato nella cronologia ({len(old_prompt)} caratteri)")
    except Exception as e:
        print(f"⚠️  Errore salvataggio cronologia: {e}")

    url = f"{ELEVENLABS_BASE}/convai/agents/{ELEVENLABS_AGENT_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {"conversation_config": {"agent": {"prompt": {"prompt": new_prompt}}}}

    try:
        resp = requests.patch(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            print(f"✅ Prompt aggiornato su ElevenLabs! ({len(new_prompt)} caratteri)")
            return True
        else:
            print(f"❌ Errore PATCH ElevenLabs: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        print(f"❌ Errore connessione ElevenLabs: {e}")
        return False


# ─────────────────────────────────────────────────────────────────
#  6. NOTIFICA EMAIL
# ─────────────────────────────────────────────────────────────────
def send_approval_email(proposal: dict, token: str, conversation_id: str = ""):
    """Invia email ad Alessio con i dettagli della proposta e i link approva/rifiuta."""
    if not SMTP_USER or not SMTP_PASSWORD:
        print("⚠️  SMTP non configurato — email non inviata")
        print(f"   Approva manualmente: {APPROVAL_BASE_URL}/approve/{token}")
        return

    approve_url = f"{APPROVAL_BASE_URL}/approve/{token}"
    reject_url  = f"{APPROVAL_BASE_URL}/reject/{token}"

    confidence   = proposal.get("confidence", 0)
    impatto      = proposal.get("impatto_stimato", "?")
    rischio      = proposal.get("rischio", "?")
    titolo       = proposal.get("titolo", "Modifica prompt")
    descrizione  = proposal.get("descrizione", "")
    diff_summary = proposal.get("diff_summary", "")
    note         = proposal.get("note_per_revisione", "")
    problemi     = "\n".join(f"• {p}" for p in proposal.get("problemi_risolti", []))

    color_conf = "#27ae60" if confidence >= 75 else "#f39c12" if confidence >= 50 else "#e74c3c"
    color_imp  = {"alto": "#e74c3c", "medio": "#f39c12", "basso": "#27ae60"}.get(impatto, "#999")
    color_risk = {"alto": "#e74c3c", "medio": "#f39c12", "basso": "#27ae60"}.get(rischio, "#999")

    html_body = f"""<!DOCTYPE html>
<html><body style="font-family: Arial, sans-serif; background: #f5f5f5; padding: 20px;">
<div style="max-width: 650px; margin: 0 auto; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
  <div style="background: linear-gradient(135deg, #1a1a2e, #16213e); padding: 30px; color: white;">
    <div style="font-size: 12px; opacity: 0.7; margin-bottom: 8px;">🤖 Centralino AI Optimizer</div>
    <h2 style="margin: 0; font-size: 20px;">💡 Nuova proposta di miglioramento</h2>
    <p style="margin: 8px 0 0; opacity: 0.85; font-size: 14px;">{titolo}</p>
  </div>
  <div style="display: flex; gap: 12px; padding: 20px; background: #f9f9f9; border-bottom: 1px solid #eee;">
    <div style="text-align: center; flex: 1;"><div style="font-size: 28px; font-weight: bold; color: {color_conf};">{confidence}%</div><div style="font-size: 11px; color: #666;">Fiducia AI</div></div>
    <div style="text-align: center; flex: 1;"><div style="font-size: 18px; font-weight: bold; color: {color_imp}; text-transform: uppercase;">{impatto}</div><div style="font-size: 11px; color: #666;">Impatto stimato</div></div>
    <div style="text-align: center; flex: 1;"><div style="font-size: 18px; font-weight: bold; color: {color_risk}; text-transform: uppercase;">{rischio}</div><div style="font-size: 11px; color: #666;">Rischio modifica</div></div>
  </div>
  <div style="padding: 24px;">
    <h3 style="color: #333; margin-top: 0;">📋 Cosa cambia</h3>
    <p style="color: #555; line-height: 1.6;">{descrizione}</p>
    <h3 style="color: #333;">🔧 Problemi risolti</h3>
    <p style="color: #555; line-height: 1.8; white-space: pre-line;">{problemi}</p>
    <h3 style="color: #333;">📝 Modifiche specifiche al prompt</h3>
    <div style="background: #f4f4f4; border-left: 4px solid #3498db; padding: 16px; border-radius: 4px; font-size: 13px; color: #444; white-space: pre-wrap;">{diff_summary}</div>
    {"<!-- Note --><h3 style='color:#e67e22;'>⚠️ Note per revisione</h3><div style='background:#fff8e1;border-left:4px solid #f39c12;padding:16px;border-radius:4px;font-size:13px;color:#555;'>"+note+"</div>" if note else ""}
    {"<p style='font-size:12px;color:#999;margin-top:16px;'>📞 Trigger: conversazione <code>"+conversation_id+"</code></p>" if conversation_id else ""}
    <div style="margin-top: 32px; display: flex; gap: 16px;">
      <a href="{approve_url}" style="flex:1;text-align:center;background:#27ae60;color:white;padding:16px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:15px;display:block;">✅ Approva e Applica</a>
      <a href="{reject_url}" style="flex:1;text-align:center;background:#e74c3c;color:white;padding:16px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:15px;display:block;">❌ Rifiuta</a>
    </div>
    <p style="font-size:11px;color:#aaa;text-align:center;margin-top:12px;">Questa proposta scade tra 48 ore. Token: {token[:12]}...</p>
  </div>
</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🤖 Proposta ottimizzazione Giulia: {titolo} ({confidence}% confidence)"
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFICATION_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    try:
        _smtp_cls = smtplib.SMTP_SSL if SMTP_PORT == 465 else smtplib.SMTP
        with _smtp_cls(SMTP_HOST, SMTP_PORT) as server:
            if SMTP_PORT != 465: server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"📧 Email inviata a {NOTIFICATION_EMAIL}")
    except Exception as e:
        print(f"❌ Errore invio email: {e}")


def send_confirmation_email(proposal: dict):
    """Email di conferma dopo l'applicazione del prompt."""
    if not SMTP_USER or not SMTP_PASSWORD:
        return
    html = f"""<html><body style="font-family: Arial; padding: 20px;">
    <div style="background: #27ae60; color: white; padding: 20px; border-radius: 8px;">
      <h2>✅ Prompt aggiornato con successo!</h2>
      <p>La modifica <strong>"{proposal.get('titolo')}"</strong> è ora attiva in produzione.</p>
      <p>L'agente Giulia risponderà con il nuovo comportamento ottimizzato.</p>
    </div></body></html>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"✅ Prompt Giulia aggiornato: {proposal.get('titolo')}"
    msg["From"] = SMTP_USER
    msg["To"]   = NOTIFICATION_EMAIL
    msg.attach(MIMEText(html, "html"))
    try:
        _smtp_cls = smtplib.SMTP_SSL if SMTP_PORT == 465 else smtplib.SMTP
        with _smtp_cls(SMTP_HOST, SMTP_PORT) as s:
            if SMTP_PORT != 465: s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        print(f"⚠️  Errore conferma email: {e}")


# ─────────────────────────────────────────────────────────────────
#  7. FUNZIONE PRINCIPALE — chiamata dal webhook
# ─────────────────────────────────────────────────────────────────
def analyze_and_propose(transcript_record: dict):
    """
    Entry point principale. Viene chiamata dopo ogni chiamata ricevuta.
    """
    conversation_id = transcript_record.get("conversation_id", "unknown")
    print(f"\n🔍 Avvio analisi AI per conversazione {conversation_id}...")

    try:
        current_prompt = get_current_prompt()
    except Exception as e:
        print(f"⚠️  Impossibile leggere prompt corrente: {e} — uso stringa vuota")
        current_prompt = ""

    single_analysis = analyze_single_call(transcript_record, current_prompt)
    single_analysis["conversation_id"] = conversation_id
    single_analysis["analyzed_at"] = datetime.now(timezone.utc).isoformat()

    analyses_file = STORAGE_DIR / "analyses_log.jsonl"
    with open(analyses_file, "a") as f:
        f.write(json.dumps(single_analysis, ensure_ascii=False) + "\n")

    proposals = load_proposals()
    pending_count = sum(1 for p in proposals.values() if p["status"] == "pending")
    if pending_count >= 2:
        print(f"⏸️  Già {pending_count} proposte in attesa — salto generazione")
        return

    recent_analyses = []
    if analyses_file.exists():
        lines = analyses_file.read_text().strip().split("\n")
        for line in lines[-CALLS_FOR_PATTERN_ANALYSIS:]:
            try:
                a = json.loads(line)
                if a.get("richiede_modifica_prompt") and a.get("confidence", 0) >= 50:
                    recent_analyses.append(a)
            except Exception:
                continue

    print(f"📊 Analisi con suggerimenti rilevanti: {len(recent_analyses)}/{CALLS_FOR_PATTERN_ANALYSIS}")

    if len(recent_analyses) < 3:
        print("⏳ Non ancora abbastanza segnali per proporre una modifica")
        return

    if single_analysis.get("confidence", 0) < CONFIDENCE_THRESHOLD and len(recent_analyses) < 5:
        print(f"🔕 Confidence troppo bassa ({single_analysis.get('confidence')}%) — aspetto più dati")
        return

    proposal = analyze_patterns_and_generate_proposal(recent_analyses, current_prompt)
    if not proposal:
        return

    if proposal.get("confidence", 0) < CONFIDENCE_THRESHOLD:
        print(f"🔕 Proposta con confidence troppo bassa ({proposal.get('confidence')}%) — scartata")
        return

    conv_ids = [a.get("conversation_id", "") for a in recent_analyses]
    token = create_proposal(proposal, conv_ids)
    send_approval_email(proposal, token, conversation_id)
    print(f"✅ Proposta creata e inviata per approvazione!")
    return token


# ─────────────────────────────────────────────────────────────────
#  8. APPLICA MODIFICA APPROVATA
# ─────────────────────────────────────────────────────────────────
def apply_approved_change(token: str) -> dict:
    """Chiamata quando l'utente clicca "Approva" nell'email."""
    proposal_record = get_proposal(token)
    if not proposal_record:
        return {"success": False, "error": "Token non trovato"}
    if proposal_record["status"] != "pending":
        return {"success": False, "error": f"Proposta già {proposal_record['status']}"}

    created = datetime.fromisoformat(proposal_record["created_at"])
    if datetime.now(timezone.utc) - created > timedelta(hours=48):
        mark_proposal(token, "expired")
        return {"success": False, "error": "Proposta scaduta (48 ore)"}

    new_prompt = proposal_record["proposal"].get("prompt_aggiornato", "")
    if not new_prompt:
        return {"success": False, "error": "Prompt aggiornato non trovato nella proposta"}

    success = update_agent_prompt(new_prompt)
    if success:
        mark_proposal(token, "approved")
        send_confirmation_email(proposal_record["proposal"])
        print(f"🚀 Prompt aggiornato in produzione!")
        return {"success": True, "message": "Prompt aggiornato su ElevenLabs"}
    else:
        return {"success": False, "error": "Errore nell'aggiornamento ElevenLabs"}


def reject_change(token: str) -> dict:
    """Marca una proposta come rifiutata."""
    proposal_record = get_proposal(token)
    if not proposal_record:
        return {"success": False, "error": "Token non trovato"}
    mark_proposal(token, "rejected")
    print(f"🚫 Proposta {token[:10]}... rifiutata")
    return {"success": True, "message": "Proposta rifiutata"}


# ─────────────────────────────────────────────────────────────────
#  VERIFY SIGNATURE (compatibilità con elevenlabs_webhook_endpoint)
# ─────────────────────────────────────────────────────────────────
def verify_elevenlabs_signature(payload_bytes: bytes, signature_header: str) -> bool:
    webhook_secret = os.environ.get("ELEVENLABS_WEBHOOK_SECRET", "")
    if not signature_header or not webhook_secret:
        return not webhook_secret
    try:
        import time
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        ts, sig = parts.get("t"), parts.get("v0")
        if not ts or not sig:
            return False
        if abs(int(time.time()) - int(ts)) > 300:
            return False
        msg = f"{ts},{payload_bytes.decode('utf-8')}"
        exp = hmac_lib.new(webhook_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return hmac_lib.compare_digest(exp, sig)
    except Exception:
        return False


def process_post_call_transcription(data: dict) -> dict:
    """Compatibilità con elevenlabs_webhook_endpoint — alias per _save_call logic."""
    conv = data.get("data", {})
    cid  = conv.get("conversation_id", "unknown")
    analysis  = conv.get("analysis", {})
    data_coll = analysis.get("data_collection_results", {})
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "conversation_id": cid,
        "agent_id": conv.get("agent_id"),
        "status": conv.get("status"),
        "duration_secs": conv.get("call_duration_secs"),
        "transcript": conv.get("transcript", []),
        "analysis": analysis,
        "metadata": conv.get("metadata", {}),
        "prenotazione": {
            "nome":     data_coll.get("nome_cliente",     {}).get("value"),
            "telefono": data_coll.get("telefono_cliente", {}).get("value"),
            "persone":  data_coll.get("numero_persone",   {}).get("value"),
            "data":     data_coll.get("data_prenotazione",{}).get("value"),
            "sede":     data_coll.get("sede",             {}).get("value"),
        },
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


if __name__ == "__main__":
    print("AI Prompt Optimizer — Test standalone")
    print(f"CLAUDE_API_KEY configurata: {'✅' if CLAUDE_API_KEY else '❌'}")
    print(f"ELEVENLABS_API_KEY:         {'✅' if ELEVENLABS_API_KEY else '❌'}")
    print(f"SMTP configurato:           {'✅' if SMTP_USER else '❌'}")
