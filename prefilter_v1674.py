"""Préfiltre thématique expérimental V16.7.4, utilisé en mode équilibré ou observation."""
import re

# Signaux suffisamment spécifiques à la thèse, même sans résumé.
DIRECT = re.compile(r"""(?ix)
    \b(?:opentitan|cva6|penglai|caliptra|ariane\s+core|keystone|sanctum|sanctorum|dorami|pmp|epmp)\b
  | \b(?:tees?|sgx|trustzone|enclaves?)\b
  | \b(?:trusted\s+execution(?:\s+environments?)?|confidential\s+computing)\b
  | \b(?:root\s+of\s+trust|secure[- ]boot|measured[- ]boot|trusted[- ]boot)\b
  | \b(?:remote|runtime|run[- ]time|continuous|hardware(?:[- ]assisted)?|control[- ]flow|execution)\s+attestation\b
  | \b(?:dice\s+(?:attestation|specification)|hardware[- ]rooted\s+(?:device\s+)?identity)\b
  | \b(?:prime\s*\+\s*probe|flush\s*\+\s*reload|spectre|meltdown|transient\s+execution)\b
  | \b(?:microarchitectur\w*\s+(?:attacks?|side[- ]channels?|leak\w*|secur\w*|vulnerabilit\w*|covert\s+channels?))\b
  | \b(?:cache\s+(?:side[- ]channels?|timing|attacks?|isolat\w*|partition\w*|leak\w*)|side[- ]channel[- ]resilient\s+caches?)\b
  | \b(?:memory\s+(?:encryption|integrity)|secure\s+memory|hardware\s+isolat\w*)\b
  | \b(?:time\s+protection|temporal\s+partition\w*|timing\s+channels?|on[- ]core\s+timing)\b
  | \b(?:secure\s+processors?|security\s+monitors?|security\s+cores?)\b
  | \b(?:physical\s+unclonable\s+functions?|pufs?)\b
""")

SPECIFIC_HARDWARE = re.compile(r"""(?ix)
    \b(?:risc[- ]?v|processors?|cpu|microarchitectur\w*|microcontrollers?|firmware|embedded\s+processors?|\n    computer\s+architecture|\n    cache|caches|llc|tlb|branch\s+predict\w*|\n    arm|x86|mips|\n    rtl|fpga|asic|vlsi|\n    system[- ]on[- ](?:a[- ])?chip|soc|silicon|chiplets?|\n    instruction[- ]set|hardware\s+security|secure\s+hardware|\n    trusted\s+computing|cryptographic\s+accelerat\w*)\b
""".replace(r'\n', ' '))
# Above literal multiline regex use re.X to ignore newlines.
SECURITY_TECH = re.compile(r"""(?ix)
    \b(?:secure|security|vulnerab\w*|attack\w*|side[- ]channels?|\n    leakage|leak\w*|mitigat\w*|defen\w*|protect\w*|\n    isolat\w*|attest\w*|confidential\w*|integrity|\n    threat\w*|privileg\w*|verif\w*|authenticat\w*|\n    cryptograph\w*|fault[- ]inject\w*|tamper\w*|trusted|\n    timing|covert\s+channels?|provenance)\b
""".replace(r'\n',' '))
# Generic hardware is not sufficient (may describe GPUs for AI, vehicle design, clinical devices).
GENERAL_HARDWARE = re.compile(r"\b(?:hardware|embedded|silicon|semiconductor|soc|chip|fpga|rtl)\b",re.I)
SURVEY = re.compile(r"\b(?:survey|review|systematic|state\s+of\s+the\s+art|sok|comparative\s+analysis)\b",re.I)

# Un titre seul peut manquer de contexte ; le mode équilibré gère ce cas dans history_multisource.py.
def proposed(title, abstract=''):
    text = ' '.join((title or '', abstract or '')).casefold()
    if len((title or '').strip()) < 7:
        return False, 'titre trop court'
    if (re.search(r'\b(?:kv[- ]cache|prefix[- ]cache)\b',text) and
        re.search(r'\b(?:llm|large language model|transformer|inference)\b',text) and
        not re.search(r'\b(?:cpu|processor|microarchitectur\w*|l1|l2|llc|risc[- ]?v|enclave|tee)\b',text)):
        return False, 'cache LLM'
    if DIRECT.search(text):
        return True, 'signal direct'
    if SPECIFIC_HARDWARE.search(text) and SECURITY_TECH.search(text):
        return True, 'matériel informatique + sécurité'
    if GENERAL_HARDWARE.search(text) and SURVEY.search(text) and re.search(r'\bsecur\w*\b',text):
        return True,'survey sécurité matérielle'
    return False,'pas de lien technique suffisant'
