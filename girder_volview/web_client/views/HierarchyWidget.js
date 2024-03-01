import HierarchyWidget from "@girder/core/views/widgets/HierarchyWidget";
import { restRequest } from "@girder/core/rest";
import { confirm } from "@girder/core/dialog";
import { wrap } from "@girder/core/utilities/PluginUtils";
import { openButton, openResources } from "./open";

const openFolder = '<i class="icon-link-ext"></i>Open Folder in VolView</a>';
const openChecked = '<i class="icon-link-ext"></i>Open Checked in VolView</a>';

function loadVolViewZip(volViewZip, resources, parentModel) {
    // found a VolView zip, update lastOpened so manifest endpoint opens it
    restRequest({
        type: "GET",
        url: `item/${volViewZip.id}/metadata`,
        contentType: "application/json",
        data: JSON.stringify({ lastOpened: new Date() }),
        method: "PUT",
        error: null,
    }).done(() => {
        openResources(parentModel, resources);
    });
}

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

        if (resources.item && resources.item.length > 0) {
            const items = resources.item.map((cid) =>
                this.itemListView.collection.get(cid)
            );
            const volViewZipsNewestFirst = items
                .filter((item) => item.attributes.name.includes(".volview.zip"))
                .sort(
                    (a, b) =>
                        new Date(b.attributes.created) -
                        new Date(a.attributes.created)
                );

            if (volViewZipsNewestFirst.length > 0) {
                const volViewZip = volViewZipsNewestFirst[0];
                // Only newest checked volview.zip item will be opened
                if (items.length >= 2) {
                    confirm({
                        text: `Will open newest VolView zip file: ${volViewZip.attributes.name}.`,
                        yesText: "Open",
                        confirmCallback: () => {
                            loadVolViewZip(
                                volViewZip,
                                resources,
                                this.parentModel
                            );
                        },
                    });
                    return;
                } else {
                    loadVolViewZip(volViewZip, resources, this.parentModel);
                    return;
                }
            }
        }
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
