"""Constanten voor BE Mijnbatterij."""
DOMAIN = "be_mijnbatterij"
VERSIE = "1.4.1"

CONF_BE_USER = "be_username"
CONF_BE_PASS = "be_password"
CONF_MB_KEY = "mb_api_key"

OPT_VERSTUREN = "versturen"
OPT_PROEF = "proef"
OPT_SALDEERT = "saldeert"
OPT_AFRR = "afrr"

STANDAARD_OPTIES = {OPT_VERSTUREN: True, OPT_PROEF: False, OPT_SALDEERT: True, OPT_AFRR: True}

POLL_S = 60
LIVE_S = 120
