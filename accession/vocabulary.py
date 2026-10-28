"""Public Library terminology; persisted decisions keep their original IDs."""
STATE_LABELS = {
    'Needs identification': 'Identity to review',
    'Needs review': 'Relationship to review',
    'Ready to register': 'PDF identity confirmed — check Zotero reference',
    'Awaiting bibliography': 'Awaiting bibliography export',
    'Linked': 'Associated in CHERQAiN; attachment observed',
    'Discrepancies': 'Relationship differs — review required',
    'Accepted differences': 'Difference accepted by you',
    'Excluded': 'Excluded from registration or automatic association',
    'Recovery': 'Observation or operation requires inspection',
    'Retired': 'File removed with recovery',
}
DEFINITIONS = {
    'association': 'Associated in CHERQAiN: a saved PDF-to-reference relationship, independent of Zotero attachments.',
    'attachment': 'Linked-file attachment in Zotero: a native attachment pointing to an external file.',
    'identity': 'PDF identity confirmed: a reviewed decision tied to the physical file evidence.',
    'parent': 'Zotero reference found: observed database/library/item identity; the citation key is an attribute.',
    'deferred': 'Deferred by you: a saved decision, not an operation scheduled to run.',
    'duplicate': 'Identical file means matching bytes, not interchangeable attachments or annotations.',
    'notes': 'In sync: the marked region of your note and the Zotero reading note say the same thing after conversion.',
}
