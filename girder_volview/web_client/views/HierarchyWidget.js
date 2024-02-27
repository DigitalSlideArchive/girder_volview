import HierarchyWidget from "@girder/core/views/widgets/HierarchyWidget";
import { wrap } from "@girder/core/utilities/PluginUtils";
import { openButton, openResources } from "./open";

const openFolder = '<i class="icon-link-ext"></i>Open Folder in VolView</a>';
const openChecked = '<i class="icon-link-ext"></i>Open Checked in VolView</a>';

wrap(HierarchyWidget, "render", function (render) {
    render.call(this);

    // Can't open/save at root of Collections, for now.
    if (this.parentModel.attributes._modelType !== "folder") {
        return;
    }
    this.$(".g-folder-header-buttons").prepend(openButton);
    const buttons = this.$el.find(".open-in-volview");
    const button = buttons[0];

    button.onclick = () => {
        const resources = JSON.parse(this._getCheckedResourceParam());
        openResources(this.parentModel, resources);
    };

    const updateChecked = () => {
        const resources = this._getCheckedResourceParam();
        button.innerHTML = resources.length >= 3 ? openChecked : openFolder;
    };
    updateChecked();

    this.listenTo(this.itemListView, "g:checkboxesChanged", updateChecked);
    this.listenTo(this.folderListView, "g:checkboxesChanged", updateChecked);
});
