def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into chunks of ~chunk_size chars with `overlap` chars shared.

    Cuts prefer whitespace so words are not split in the middle.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap must be >= 0 and < chunk_size")

    text = text.strip()
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + chunk_size // 2, end)
            cut = max(cut, text.rfind("\n", start + chunk_size // 2, end))
            if cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        next_start = max(end - overlap, start + 1)
        # Don't begin the next chunk in the middle of a word.
        while next_start < end and not text[next_start - 1].isspace():
            next_start += 1
        start = next_start
    return chunks
