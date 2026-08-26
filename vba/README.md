# VBA macros for Excel and PowerPoint

Importable VBA modules plus a ribbon kit. The three macro modules are
self-contained — import only the ones you want. The two `_MGRibbon` modules
are optional glue for the ribbon tab and shortcuts, and must be imported
alongside the macro modules they call.

| File | App | What it does |
| --- | --- | --- |
| `Excel_AutoColor.bas` | Excel | Color cell fonts by content type (hardcode / formula / sheet link / external link) |
| `PPT_ShapeTools.bas` | PowerPoint | Grab & apply shape dimensions, swap two shapes, insert/remove TBU markers |
| `PPT_Rider.bas` | PowerPoint | Rip the current slide into a `<Deck> - Rider.pptx` scrap file |
| `Excel_MGRibbon.bas` | Excel | Ribbon-button dispatch + `Ctrl+Shift` keyboard shortcuts |
| `PPT_MGRibbon.bas` | PowerPoint | Ribbon-button dispatch for the MG Macros tab |
| `ribbon/…customUI14.xml` | both | The "MG Macros" ribbon tab definitions (buttons, groups, keytips) |
| `ribbon/Install-MGRibbon.ps1` | both | One-command installer that embeds the tab into your add-in |

Two ways to run them: the quick import below (macros via `Alt+F8`, QAT
buttons, Excel shortcuts), or the full **MG Macros ribbon tab** — see
[its own section](#the-mg-macros-ribbon-tab--shortcuts) further down.

## Importing

### Excel — put it in your Personal Macro Workbook so it's always available

1. If you've never used PERSONAL.XLSB: record a throwaway macro once
   (View → Macros → Record Macro → store in **Personal Macro Workbook** → stop
   recording). This creates the workbook.
2. Press `Alt+F11` to open the VBA editor.
3. In the Project pane, click **VBAProject (PERSONAL.XLSB)**, then
   **File → Import File…** and pick `Excel_AutoColor.bas`.
4. Save (Ctrl+S inside the VBA editor) and you're done — the macros are
   available in every workbook.

Optional keyboard shortcut: View → Macros → select `AutoColorSelection` →
**Options…** → assign e.g. `Ctrl+Shift+A`.

### PowerPoint — keep the macros in a small .pptm you leave open

PowerPoint has no personal macro workbook, so the usual trick is a tiny
macro-enabled deck (e.g. `MyMacros.pptm`) that you open when you work:

1. Create a new blank presentation and save it as **PowerPoint
   Macro-Enabled Presentation (.pptm)**.
2. Press `Alt+F11`, select its project, **File → Import File…**, and import
   `PPT_ShapeTools.bas` and `PPT_Rider.bas`.
3. Save. While `MyMacros.pptm` is open, the macros work on whatever
   presentation is active.

Quick Access Toolbar buttons (recommended — one click per macro):
File → Options → **Quick Access Toolbar** → "Choose commands from:
**Macros**" → add the ones you use (GrabShapeSize, ApplyWidth, ApplyHeight,
ApplySize, SwapShapes, InsertTBUMarker, SendSlideToRider, …).

> Copy-paste instead of importing? Fine — but don't paste the first line
> (`Attribute VB_Name = "…"`); it's file metadata, not code, and won't compile
> inside a module.

If macros are blocked: right-click the downloaded file → Properties →
**Unblock**, and check File → Options → Trust Center → Macro Settings.

## The macros

### Excel_AutoColor

- **`AutoColorSelection`** — colors the fonts of the selected cells.
- **`AutoColorActiveSheet`** — same, for the sheet's whole used range.

Convention (standard modeling colors, changeable in the CONFIG block):

| Font color | Meaning |
| --- | --- |
| Blue | Hardcoded number typed into the cell |
| Black | Formula referencing only its own sheet |
| Green | Formula referencing another sheet in the workbook |
| Red | Formula linking to a different workbook |

Details worth knowing:

- Text labels are left untouched by default (`COLOR_TEXT_CONSTANTS = False`).
- Re-running is safe: a stale blue/green/red left over from a previous run is
  reset when the cell's content changed category, while your own label/header
  styling is never touched (only the three convention colors get reset).
- The detection is careful about the tricky cases: string literals
  (`="Total!"`), structured table references (`=[@[Adj. EBITDA]]/Drivers!C5`
  stays green, not red), same-sheet refs (`=Sheet1!A1` on Sheet1 stays black),
  sheet names that are suffixes of each other (`Model` vs `DCF_Model`), and
  bracket-free external links (`=Assumptions.xlsx!WACC`,
  `='C:\Deals\Assumptions.xlsx'!WACC`) all classify correctly.

### PPT_ShapeTools

**Size grabber** — a format-painter for dimensions:

1. Select one shape → run **`GrabShapeSize`** (silent; set
   `CONFIRM_GRAB = True` in the CONFIG block for a popup).
2. Select any other shape(s) → run **`ApplyWidth`**, **`ApplyHeight`**, or
   **`ApplySize`** for both. Aspect-ratio lock is handled automatically, and
   "resize shape to fit text" (autofit) is turned off on the targets so the
   applied size actually sticks. The grabbed size survives until you close
   PowerPoint.

**Swap** — select exactly two shapes:

- **`SwapShapes`** — the shapes trade places center-for-center (best for
  flipping two text boxes on opposite sides of a page, even when their sizes
  differ).
- **`SwapShapesTopLeft`** — trades top-left corners instead, if you prefer
  edge-anchored swapping.

**TBU marker**:

- **`InsertTBUMarker`** — drops a yellow, red-bordered, bold-red "TBU" text
  box pinned to the top-right corner of the selected shape (or of the slide
  when nothing is selected). Repeated markers cascade slightly so they never
  hide behind each other. Text, font, size and colors are in the CONFIG block.
- **`RemoveTBUMarkersOnSlide`** / **`RemoveTBUMarkersInDeck`** — markers are
  tagged internally, so cleanup removes exactly the ones this macro created —
  including markers you've since grouped with other shapes — and never touches
  a normal text box that happens to say "TBU".

### PPT_Rider

- **`SendSlideToRider`** — copies the slide you're on into
  `<Deck name> - Rider.pptx`, next to the deck. Creates the rider on first
  use, appends on every use after that, and preserves the slide's
  design/master/colors.
- **`MoveSlideToRider`** — same, then deletes the slide from the deck —
  "section it out". The slide is only deleted after the rider has saved
  successfully.

Behavior notes:

- Decks on OneDrive/SharePoint report a web URL instead of a folder, so their
  rider goes to your real Documents folder (folder redirection and OneDrive
  Known Folder Move are honored) with a short tag in the name — e.g.
  `Pitch - Rider (3F2A).pptx` — so identically named decks in different deal
  folders get separate riders.
- If the rider is already open with unsaved edits of yours, the macro never
  silently saves over them: a copy is appended and left unsaved for you, and a
  move is refused until you've saved or discarded — so a ripped slide can
  never exist only in an unsaved file.

## The MG Macros ribbon tab & shortcuts

The `ribbon/` folder turns the macros into a proper **"MG Macros" tab next to
Home**, with grouped buttons like the built-in tabs, plus keyboard access.
Office compiles VBA only when *it* saves a file, so there's a one-time
assembly step per app (~5 minutes); everything else is prebuilt.

### Excel: build `MGMacros.xlam`

1. Open Excel with a blank workbook → `Alt+F11` → File → Import File… →
   import **both** `Excel_AutoColor.bas` and `Excel_MGRibbon.bas` into the
   blank workbook's project.
2. Back in Excel: File → Save As → type **Excel Add-in (*.xlam)** → name it
   `MGMacros.xlam` (Excel jumps to your AddIns folder — fine). Close Excel.
3. Embed the tab:
   `powershell -ExecutionPolicy Bypass -File Install-MGRibbon.ps1 -File "<path>\MGMacros.xlam"`
   (run from the `ribbon/` folder; it writes a `.bak` backup first).
4. Reopen Excel → File → Options → Add-ins → Manage **Excel Add-ins** → Go →
   check **MGMacros** (Browse to it if it isn't listed).

You now have the **MG Macros tab** in every workbook, plus real shortcuts:
**`Ctrl+Shift+A`** (AutoColor selection) and **`Ctrl+Shift+S`** (whole
sheet), bound automatically when the add-in loads. To change them, edit the
CONFIG block at the top of `Excel_MGRibbon.bas` (`^`=Ctrl, `+`=Shift,
`%`=Alt) and re-save the add-in. Keytips work too: `Alt, G, A` — in Excel
the tab answers to `G`, because the built-in Formulas tab owns the bare `M`
keytip and an `M`-starting sequence can never reach a custom tab.

### PowerPoint: build `MGMacros.ppam`

1. Open PowerPoint with a blank presentation → `Alt+F11` → import **all
   three**: `PPT_ShapeTools.bas`, `PPT_Rider.bas`, `PPT_MGRibbon.bas`.
2. File → Save As → type **PowerPoint Add-in (*.ppam)** → `MGMacros.ppam`.
   Also save a copy as `MGMacros.pptm` — that's your editable master for
   future changes, since a `.ppam` can't be reopened for editing.
   Close PowerPoint.
3. Embed the tab:
   `powershell -ExecutionPolicy Bypass -File Install-MGRibbon.ps1 -File "<path>\MGMacros.ppam"`
4. Reopen PowerPoint → File → Options → Add-ins → Manage **PowerPoint
   Add-ins** → Go → Add New… → pick `MGMacros.ppam`.

The tab loads in every session with all eleven buttons. Keyboard access in
PowerPoint (which has no `OnKey` API, so no direct Ctrl-combos):

- **Keytips**: `Alt, M G`, then the button's letter — `G` grab size,
  `W`/`H`/`S` apply width/height/both, `P` swap, `T` TBU, `C`/`M`
  copy/move to rider. Every letter is a `keytip="…"` attribute in
  `PPT_MGMacros_customUI14.xml` — change them there and re-run the
  installer.
- **QAT numbers**: right-click any MG button → *Add to Quick Access
  Toolbar*; QAT slots answer to `Alt+1` … `Alt+9`.

### Customizing the tab itself

Button labels, order, groups, icons (`imageMso`), tooltips and keytips all
live in the two `customUI14.xml` files — edit and re-run the installer, which
replaces the embedded part cleanly. If PowerShell is locked down on your
machine, the free **Office RibbonX Editor** does the same job: open the
add-in, Insert → Office 2010+ Custom UI Part, paste the XML, save.

## Notes

- The `.bas` files use Windows (CRLF) line endings on purpose — the VBA
  editor's import expects them.
- Windows Office is assumed (paths like `Documents\` in PPT_Rider).
- Everything configurable sits in a marked CONFIG block at the top of each
  module: colors, TBU text/fonts/sizes, the rider filename suffix.
