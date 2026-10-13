import re
import xml.etree.ElementTree as ET

_DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _parse_xml(document: bytes) -> ET.Element:
    if _DOCTYPE_RE.search(document):
        raise ValueError("XML with a DOCTYPE or ENTITY declaration is not accepted")
    try:
        return ET.fromstring(document)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML: {exc}") from exc
