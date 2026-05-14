# Example SimplePyPad customization file.
# Copy this to your user config or launch with:
#   python simplepypad.py --config examples/init.py

api.set_font("Consolas", 12)
api.set_theme("friendly")
api.set_option("tab_width", 4)


def duplicate_line(api):
    text = api.text_widget
    start = text.index("insert linestart")
    end = text.index("insert lineend")
    line = text.get(start, end)
    text.insert(end, "\n" + line)


api.add_command("custom.duplicate_line", duplicate_line)
api.add_menu_item("Tools", "Duplicate Line", "custom.duplicate_line")
api.bind_key("<Control-d>", lambda api, event: (api.run_command("custom.duplicate_line"), "break")[-1])
