"""Test doubles shared across test modules."""
from __future__ import annotations

HINDI_TO_EN = {
    "पंजाब विधानसभा से कुंवर विजय प्रताप सिंह ने दिया इस्तीफा": "Kunwar Vijay Pratap Singh resigns from Punjab Assembly",
    "दिल्ली में प्रदूषण का स्तर खतरनाक": "Pollution level in Delhi dangerous",
    "कुंवर विजय प्रताप सिंह ने सोमवार को पंजाब विधानसभा से इस्तीफा दे दिया।":
        "Kunwar Vijay Pratap Singh on Monday resigned from the Punjab Assembly.",
    "वह इस साल आम आदमी पार्टी छोड़ने वाले तीसरे विधायक हैं।":
        "He is the third MLA to leave the Aam Aadmi Party this year.",
    "उन्होंने अपना इस्तीफा चंडीगढ़ में स्पीकर को सौंपा।": "He submitted his resignation to the Speaker in Chandigarh.",
    "पार्टी नेताओं ने कहा कि सरकार के बहुमत पर कोई असर नहीं पड़ेगा।":
        "Party leaders said the government's majority would not be affected.",
    "विपक्ष ने तुरंत उपचुनाव कराने की मांग की।": "The opposition demanded an immediate by-election.",
    "दिल्ली में सोमवार को वायु गुणवत्ता सूचकांक 430 के पार पहुंच गया।":
        "Air quality index in Delhi crossed 430 on Monday.",
    "पराली के धुएं के कारण हवा बेहद खराब श्रेणी में पहुंच गई।":
        "Stubble smoke pushed the air into the very poor category.",
    "स्कूलों को ऑनलाइन कक्षाएं चलाने के निर्देश दिए गए हैं।": "Schools have been told to run online classes.",
}


class PhraseTableBackend:
    """Offline stand-in for a real MT model: sentence-level lookup. Records its calls."""

    def __init__(self, table=None, fail_on=None):
        self.table = table or HINDI_TO_EN
        self.calls = []
        self.fail_on = fail_on

    def translate(self, text, src, tgt):
        from newspaper_pipeline.clean.translate import TranslationError
        self.calls.append((text, src, tgt))
        if self.fail_on and self.fail_on in text:
            raise TranslationError("boom")
        import re
        out = []
        for sent in re.split(r"(?<=[.!?\u0964])\s+", " ".join(text.split())):
            out.append(self.table.get(sent, f"[{src}->{tgt}] {sent}"))
        return " ".join(out)
