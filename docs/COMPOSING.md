# Visual PDF composition

The composer creates a new PDF from selected original pages. It does not edit text, add text boxes, perform OCR, or overwrite source documents.

## Default: drag, arrange, save

The default view has two panes and three buttons: **Add PDF**, **Save PDF** and **Advanced composition**. There is no tree, page-range field or insertion toolbar to configure.

Drop the main PDF onto the empty left pane to start with its pages. Additional files become materials. Drop material PDFs onto the right, or use Add PDF; pick a document in the dropdown and drag its pages into the left result pane. The colored insertion marker indicates the gap. Dragging existing result pages moves them rather than creating duplicates. Dropping more files onto a non-empty result only adds materials; whole-document concatenation is available in Advanced.

Use Ctrl/Shift to select multiple pages. Scroll continuously through thumbnails; dragging near the top/bottom scrolls the result. Double-click a page for a zoomable preview. Hover over a result card and click **×**, or select pages and press Delete, to omit them only from the new PDF. Ctrl+Z undoes changes; Ctrl+Shift+Z or Ctrl+Y redoes them. Right-click also offers undo/redo and removal.

Save PDF opens a standard save dialog. Cancelling leaves the entire plan intact. The default hands the saved document to the compression workspace; it does not start lossy compression automatically. Advanced contains the save-only choice and output-path controls.

Switching modes preserves the exact tree, including group IDs, page order, bookmarks, undo history and save settings. Simple-mode insertion targets the parent of the following output page; appending creates leaves at the root. Moving pages in simple mode can therefore move them across groups, but switching modes alone never flattens the tree.

## Advanced: three-pane workspace

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
- The material library has no fixed file-count cap. Metadata is checked in batches of up to 64. Simple-mode lists use model-backed cards, batched layout and visible-page thumbnail requests; advanced browsers show at most 24 pages per batch. Each thumbnail job renders at most 12 pages and the cache is bounded to 192 entries. Output is limited to 20,000 pages and a 12-level tree. Individual sources are limited to 4 GiB; sources actually used by one composition are limited to 16 GiB in total. Large jobs may require splitting even within these limits.
- Inspection, page rendering and writing run in cancellable processes. The parent owns the temporary workspace and final output installation. These processes are not a filesystem/network security sandbox.

The whole-file queue is available only under **Advanced composition → Whole-document merge** and retains its 100-source limit. It preloads the material library (subject to that limit). Cancelling returns to the unchanged page plan. Completing the queue exports the queued documents instead of the selected-page plan. All loaded materials and the active compression document remain protected against overwrite.
