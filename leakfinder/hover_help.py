"""Small, pointer-triggered help for report controls and table cells."""

import tkinter as tk


class HoverHelp:
    def __init__(self, widget, describe, delay=500):
        self.widget = widget
        self.describe = describe
        self.delay = delay
        self.timer = None
        self.popup = None
        self.text = None
        self.point = None
        for event in ("<Motion>", "<Enter>"):
            widget.bind(event, self.motion, add="+")
        for event in (
            "<Leave>",
            "<ButtonPress>",
            "<MouseWheel>",
            "<Unmap>",
            "<Destroy>",
        ):
            widget.bind(event, self.hide, add="+")

    def motion(self, event):
        text = self.describe(event)
        self.point = event.x_root, event.y_root
        if text == self.text:
            return
        self.hide()
        self.text = text
        if text:
            self.timer = self.widget.after(self.delay, self.show)

    def hide(self, _=None):
        if self.timer:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.popup:
            self.popup.destroy()
            self.popup = None
        self.text = None

    def show(self):
        self.timer = None
        popup = self.popup = tk.Toplevel(self.widget, takefocus=0)
        popup.withdraw()
        popup.wm_overrideredirect(True)
        popup.wm_attributes("-topmost", True)
        tk.Label(
            popup,
            text=self.text,
            justify="left",
            wraplength=340,
            font=("Segoe UI", 10),
            background="#ffffff",
            foreground="#192f43",
            highlightbackground="#8194a6",
            highlightthickness=1,
            padx=9,
            pady=6,
        ).pack(fill="both", expand=True)
        popup.update_idletasks()
        x, y = self.point
        width, height = popup.winfo_reqwidth(), popup.winfo_reqheight()
        x = max(0, min(x + 13, popup.winfo_screenwidth() - width - 8))
        y = (
            y + 20
            if y + height + 28 <= popup.winfo_screenheight()
            else max(0, y - height - 9)
        )
        popup.geometry(f"+{x}+{y}")
        popup.deiconify()


def tree_help(tree, describe):
    def cell(event):
        if tree.identify_region(event.x, event.y) not in {"cell", "heading"}:
            return None
        column = tree.identify_column(event.x)
        if not column or column == "#0":
            return None
        return describe(tree.column(column, "id"), tree.identify_row(event.y))

    return HoverHelp(tree, cell)


CONFIDENCE_HELP = (
    "A 95% confidence interval shows sampling uncertainty. Over many similar samples, "
    "about 95% of intervals made this way would cover the underlying value, assuming "
    "the method's conditions hold. Wider intervals mean less precision."
)
