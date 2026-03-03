{
  "type": "webhook",
  "name": "book_table",
  "description": "Verifica disponibilità (fase=availability) o registra una prenotazione (fase=book). Regole: usare sempre availability prima di book; orario sempre HH:MM; seggiolini solo se richiesti (max 2).",
  "disable_interruptions": false,
  "force_pre_tool_speech": "auto",
  "tool_call_sound": null,
  "tool_call_sound_behavior": "auto",
  "tool_error_handling_mode": "auto",
  "execution_mode": "immediate",
  "api_schema": {
    "url": "https://centralino-webhook-production.up.railway.app/book_table",
    "method": "POST",
    "path_params_schema": [],
    "query_params_schema": [],
    "request_body_schema": {
      "id": "body",
      "type": "object",
      "description": "Dati per disponibilità/prenotazione",
      "required": true,
      "value_type": "llm_prompt",
      "properties": [
        {
          "id": "fase",
          "type": "string",
          "value_type": "llm_prompt",
          "description": "availability (controllo) oppure book (prenotazione finale).",
          "dynamic_variable": "",
          "enum": ["availability", "book"],
          "is_system_provided": false,
          "required": true
        },
        {
          "id": "data",
          "type": "string",
          "value_type": "llm_prompt",
          "description": "Data in formato YYYY-MM-DD (se relativa: già confermata).",
          "dynamic_variable": "",
          "enum": null,
          "is_system_provided": false,
          "required": true
        },
        {
          "id": "orario",
          "type": "string",
          "value_type": "llm_prompt",
          "description": "Orario in formato HH:MM.",
          "dynamic_variable": "",
          "enum": null,
          "is_system_provided": false,
          "required": true
        },
        {
          "id": "persone",
          "type": "string",
          "value_type": "llm_prompt",
          "description": "Numero persone (1–9). Esempio: \"2\".",
          "dynamic_variable": "",
          "enum": null,
          "is_system_provided": false,
          "required": true
        },
        {
          "id": "seggiolini",
          "type": "string",
          "value_type": "llm_prompt",
          "description": "Numero seggiolini (0–2). Se non richiesti: \"0\".",
          "dynamic_variable": "",
          "enum": null,
          "is_system_provided": false,
          "required": false
        },
        {
          "id": "sede",
          "type": "string",
          "value_type": "llm_prompt",
          "description": "Talenti, Appia, Ostia Lido, Reggio Calabria, Palermo.",
          "dynamic_variable": "",
          "enum": ["Talenti", "Appia", "Ostia Lido", "Reggio Calabria", "Palermo"],
          "is_system_provided": false,
          "required": true
        },
        {
          "id": "nome",
          "type": "string",
          "value_type": "llm_prompt",
          "description": "Nome cliente (obbligatorio solo in fase=book).",
          "dynamic_variable": "",
          "enum": null,
          "is_system_provided": false,
          "required": false
        },
        {
          "id": "telefono",
          "type": "string",
          "value_type": "llm_prompt",
          "description": "Telefono (solo cifre). Obbligatorio solo in fase=book.",
          "dynamic_variable": "",
          "enum": null,
          "is_system_provided": false,
          "required": false
        },
        {
          "id": "email",
          "type": "string",
          "value_type": "llm_prompt",
          "description": "Email (opzionale). Se vuota: server usa default.",
          "dynamic_variable": "",
          "enum": null,
          "is_system_provided": false,
          "required": false
        },
        {
          "id": "note",
          "type": "string",
          "value_type": "llm_prompt",
          "description": "Note (allergie/richieste). Se nessuna: stringa vuota.",
          "dynamic_variable": "",
          "enum": null,
          "is_system_provided": false,
          "required": false
        }
      ]
    },
    "request_headers": [
      { "type": "value", "name": "Content-Type", "value": "application/json" }
    ],
    "auth_connection": null
  },
  "assignments": [],
  "response_timeout_secs": 120,
  "dynamic_variables": { "dynamic_variable_placeholders": {} }
}
