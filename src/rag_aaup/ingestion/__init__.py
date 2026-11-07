"""Ingestion: turn raw documents (PDF, DOCX, PPTX, XLSX, HTML, TXT, scanned images) into plain text.

Input:  files in data/raw/
Output: one record per document (text + metadata such as source file, page, title).
OCR for scanned Arabic pages belongs here too.
"""
