"""Contrôles rapides sans Gemini ni API (exécuter `python test_prefilter.py`)."""
from prefilter_v1674 import proposed

positifs = [
    'Modern Hardware Security: A Review of Attacks and Countermeasures',
    'Time Protection: The Missing OS Abstraction',
    'DR.SGX: automated and adjustable side-channel protection for SGX',
    'Sanctum: Minimal Hardware Extensions for Strong Software Isolation',
    'Hardware Lazy PAT: Making Memory Encryption with Integrity Verification Unobtrusive',
]
negatifs = [
    'Does rural digitalization improve food production security?',
    'In Referees We Trust?',
    'Hardware-Aware Dynamic Quantization for DNN Inference',
    'Hardware-Aware Fast-Charging Framework for EV Battery Management',
]
for title in positifs:
    assert proposed(title)[0], f'Référence pertinente rejetée : {title}'
for title in negatifs:
    assert not proposed(title)[0], f'Bruit conservé : {title}'
print('OK :', len(positifs), 'titres de référence et', len(negatifs), 'contre-exemples.')
