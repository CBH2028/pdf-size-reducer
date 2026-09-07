# Visual PDF composition

The composer creates a new PDF from selected original pages. It does not edit text, add text boxes, perform OCR, or overwrite source documents.

## Three-pane workspace

- **Left: main PDF and output preview.** Loading the initial main PDF adds its pages as a branch when the plan is empty. Changing the main source later changes the browser without clearing or replacing the existing tree. The output-preview tab shows the tree's current page sequence.
- **Middle: composition tree.** The root represents the new PDF. Groups can contain pages and other groups; a page is always a leaf. Output order is depth-first, top to bottom. The second column shows output page numbers; leaf labels and tooltips identify original pages and source files. Non-empty groups become PDF bookmarks.
- **Right: material library.** Add files repeatedly, drag PDFs from Explorer, search names, and select a material to browse its pages. Adding a material alone never changes the output. Ctrl/Shift-select whole files to insert several document branches together, or drag them directly to the tree.

## Inserting and arranging pages

Select thumbnail pages with Ctrl/Shift. For selections across thumbnail batches, enter ranges such as `1, 3-5, 30` and click the page-selection button. Descending ranges such as `5-3` retain their entered order; duplicate page numbers within one selection are removed. Insert the same page again if an intentional duplicate is needed in the final PDF.

Drag pages from either source pane into the tree. The top/bottom part of a row inserts before/after that row; the middle of a group inserts inside it. Dropping on the root or empty tree area appends at the end. The insertion-mode dropdown controls **button-based** insertion, not drag location.

Drag tree pages or groups to move them across branches. An ancestor and its selected descendants move as one branch. Groups cannot be dropped into themselves or their descendants. Up/Down buttons and Alt+Up/Down move selected nodes within their current parent; Delete removes nodes only from the plan. Undo/redo covers page insertion, whole-document batch insertion, grouping, naming, moving and removal. History retains up to 40 actions, with a smaller history for large trees to bound memory use.

Double-click any page for an isolated high-resolution preview. Use the wheel to zoom and drag to pan. Select a page in the tree to reveal its position in the live output-preview pane. The live preview displays the original page content in final output order; it does not repeatedly write intermediate PDFs.

## Saving and compression

Choose a new destination and generate the PDF. The default filename avoids existing paths. All loaded sources and the active compression document are protected against overwrite. Declining an overwrite keeps the tree open. Save-only leaves the new PDF on disk; save-and-compress loads it into the existing Figure-preview workspace so a size target can be chosen. Compression saves a separate result.

Source file identity, modification time and size are checked at inspection, before processing, and before final installation. If a source changes, recheck the library and confirm the original page numbers in the tree. Rechecking retains the tree; missing/out-of-range pages block saving until corrected. These checks detect ordinary changes, not malicious filesystem races or spoofed metadata.

## Content preservation and limits

- Pages are copied as PDF content, not rasterized. Text stays searchable; page dimensions, rotation, ordinary annotations and supported AcroForm widgets are retained.
- Ordinary internal page links are remapped to included output pages. Links to omitted pages are removed. When a source page appears more than once, an internal link targets its first output occurrence. Ordinary external links retain their original targets.
- The composition tree defines output bookmarks. Source document outlines are not automatically merged into these tree-defined bookmarks. Basic metadata comes from the first source used in output order.
- Document-level attachments, PDF portfolios, digital signatures, named destinations and accessibility structure are not guaranteed to survive page composition. Review important documents before use. Password-protected inputs must be decrypted first.
- Compatible plans made entirely of complete documents continue to use the existing guarded Rust/C++ merger, with fallback as before. Arbitrary selected-page plans use an isolated, form-aware PyMuPDF writer. This does not claim a new speedup or route arbitrary page insertion through the Rust guard.
- The material library has no fixed file-count cap. Metadata is checked in batches of up to 64, page browsers show at most 24 thumbnails per batch, and the thumbnail cache is bounded to 192 entries. Output is limited to 20,000 pages and a 12-level tree. Individual sources are limited to 4 GiB; sources actually used by one composition are limited to 16 GiB in total. Large jobs may require splitting even within these limits.
- Inspection, page rendering and writing run in cancellable processes. The parent owns the temporary workspace and final output installation. These processes are not a filesystem/network security sandbox.

The old whole-file queue remains available through **Quick whole-document merge** and retains its 100-source limit.
