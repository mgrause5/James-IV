# VBA macros for Excel and PowerPoint

Three importable modules. Each is self-contained — import only the ones you want.

| File | App | What it does |
| --- | --- | --- |
| `Excel_AutoColor.bas` | Excel | Color cell fonts by content type (hardcode / formula / sheet link / external link) |
| `PPT_ShapeTools.bas` | PowerPoint | Grab & apply shape dimensions, swap two shapes, insert/remove TBU markers |
| `PPT_Rider.bas` | PowerPoint | Rip the current slide into a `<Deck> - Rider.pptx` scrap file |

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

Text labels are left untouched by default (`COLOR_TEXT_CONSTANTS = False`).
String literals inside formulas (`="Total!"`) don't fool the detection, and
`=Sheet1!A1` written *on* Sheet1 stays black.

### PPT_ShapeTools

**Size grabber** — a format-painter for dimensions:

1. Select one shape → run **`GrabShapeSize`** (silent; set
   `CONFIRM_GRAB = True` in the CONFIG block for a popup).
2. Select any other shape(s) → run **`ApplyWidth`**, **`ApplyHeight`**, or
   **`ApplySize`** for both. Aspect-ratio lock is handled automatically, and
   the grabbed size survives until you close PowerPoint.

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
  tagged internally, so cleanup removes exactly the ones this macro created
  and never touches a normal text box that happens to say "TBU".

### PPT_Rider

- **`SendSlideToRider`** — copies the slide you're on into
  `<Deck name> - Rider.pptx`, next to the deck (or in `Documents\` when the
  deck lives on OneDrive/SharePoint). Creates the rider on first use, appends
  on every use after that, and preserves the slide's design/master/colors.
- **`MoveSlideToRider`** — same, then deletes the slide from the deck —
  "section it out". The slide is only deleted after the rider has saved
  successfully.

## Notes

- The `.bas` files use Windows (CRLF) line endings on purpose — the VBA
  editor's import expects them.
- Windows Office is assumed (paths like `Documents\` in PPT_Rider).
- Everything configurable sits in a marked CONFIG block at the top of each
  module: colors, TBU text/fonts/sizes, the rider filename suffix.
