"""Conservative matching keys for financial table titles; never alter source text."""
import re


NOTE_PREFIX = re.compile(r'^note\s+\d+(?:\s*[.:)–—-]\s*|\s+)(?=\S)', re.I)


def normalize_table_title(title: str) -> str:
    """Ignore a leading numbered note, case, and whitespace only.

    'Note 8. Property, Plant, and Equipment' matches its unnumbered title or a
    different note number in another year. Bare 'Note 8', 'Notes Payable', and
    numbers elsewhere in the title retain their meaning.
    """
    return NOTE_PREFIX.sub('', ' '.join(title.split())).casefold()
