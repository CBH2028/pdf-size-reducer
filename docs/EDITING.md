# PDF editing and page deletion

PDF Size Reducer v3.10.0 adds a copy-on-save editor. Open **编辑 PDF · 自动分区** in the main window. All PDF processing stays local.

## Workflow

1. Select a page. Blue outlines identify text regions; gray outlines identify image areas. Region detection uses PDF text blocks, line geometry, spacing and column separation. It does not infer a document's complete semantic structure.
2. Click a text region to replace it. An empty replacement deletes that region's original text. Use **框选新增文字** to drag a new box, with a style chosen from nearby text. Choose another reference region to match a different paragraph.
3. Adjust the text, size, RGB color, alignment, line spacing or box position/size. Measurements are PDF points, in the unrotated page coordinate system; overlays and mouse selection are transformed correctly for rotated pages.
4. Click **应用到草稿并预览**. Preview and save use the same typesetting code. Green outlines are pending changes, and the orange outline marks the active box. Changes inside the input field must be applied before switching regions or pages. Saving unapplied input first requests preview confirmation.
5. Use **删除本页** / **恢复本页** or **页面管理…** to mark original page numbers for removal. Examples: `2`, `2, 4-6`, `1 3 5`. Ranges are inclusive. Emptying the list restores all pages. Invalid ranges and deleting every page are rejected. Page numbering remains the source numbering until export; drafts on deleted pages are ignored on export and return if those pages are restored.
6. Undo/redo changes the draft, including page deletion. **另存为 PDF** saves a separate file; **保存并继续压缩** also loads it into the accelerated compression workspace. Review that output before submitting or distributing it.

## Typography and preservation

- Embedded font programs are preferred when available and when they contain every required character. Otherwise the editor tries known local font families and then built-in Latin/CJK fallbacks. A fallback is reported in the preview and save result; it is not represented as an exact match.
- Original font size, color and baseline are retained for supported replacements. Nearby text supplies the initial size, color, alignment and line-spacing settings for additions. New lines wrap inside the selected box, without automatic font-size reduction or silently omitted overflow.
- Mixed-font/style regions use the dominant style when replaced; this is explicitly noted. The editor does not preserve arbitrary per-character formatting, kerning, complex shaping or typographic effects.
- Text is removed through text-only redaction, without white background patches or removal of underlying image/vector content. Neighboring text and form overlaps are checked, and intersecting page links removed by redaction are restored. Pages are not rasterized on export; inserted text remains searchable/copyable.
- Page deletion updates retained internal link targets. Links pointing to a removed page are removed, and bookmarks to it are disabled rather than reassigned to an unrelated page. See [PyMuPDF's page-deletion behavior](https://pymupdf.readthedocs.io/en/latest/the-basics.html#deleting-pages).
- Source files are never overwritten. Existing outputs are replaced only after the completed candidate passes validation and the final source-state/cancellation check.

## Limits and safety

This is not OCR or a full document layout engine. Scanned/image text, structured table editing, vertical/rotated text runs, invisible or translucent text, unsafe overlaps, and complex-script shaping are not supported for direct replacement. Page rotation itself is supported. Image areas can receive added overlay text, but this does not remove characters already present in the image. Geometric segmentation can split or group content differently from an authoring application, so inspect each selected region.

Documents with signature fields cannot be edited or have pages removed in this version, including unsigned signature fields. Existing redaction annotations on an edited page are rejected to avoid applying unrelated deletions. Password-protected inputs must be decrypted first. Editing a tagged/accessibility-structured PDF does not regenerate its semantic structure or logical reading order; validate accessibility separately.

Preview, analysis and saving run in cancellable child processes under the existing parent-owned Windows lifetime job. The parent owns the final output rename. These are PyMuPDF operations, not new commands in the Rust/C++ accelerator, and the lifetime job is not an OS sandbox. Source checks use filesystem metadata, not file locks or cryptographic change detection. Limits include 4 GiB input, 1,000 active text edits, 50,000 characters per region, 500,000 characters per operation and a 12-megapixel preview. See [SECURITY.md](../SECURITY.md).

## Verification

Run the full regression suite with `python -m pytest -q`. Source or packaged builds support `--editor-self-test`, covering the real Qt editor, isolated previews, matching-text addition, replacement, page deletion, saving and compression handoff. `--workflow-self-test` still covers guarded merging/compression, preview cancellation and the native Figure planner.

The typography implementation uses PyMuPDF's [text/span metadata](https://pymupdf.readthedocs.io/en/latest/textpage.html#span-dictionary) and [text-only redaction controls](https://pymupdf.readthedocs.io/en/latest/page.html#Page.apply_redactions). Source installations now require PyMuPDF 1.26 or newer.
