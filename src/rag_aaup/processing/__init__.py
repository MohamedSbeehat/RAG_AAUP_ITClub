"""Processing: clean, normalize and chunk the extracted text.

- Arabic normalization (diacritics, tatweel, alef/ya/ta-marbuta variants)
- Chunking with overlap, keeping metadata (source, page, section) on every chunk

Output is written to data/processed/.
"""
