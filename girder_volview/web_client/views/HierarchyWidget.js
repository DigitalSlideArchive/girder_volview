import HierarchyWidget from "@girder/core/views/widgets/HierarchyWidget";
import { restRequest } from "@girder/core/rest";
import { confirm } from "@girder/core/dialog";
import { wrap } from "@girder/core/utilities/PluginUtils";
import { openButton, openResources } from "./open";

const openFolder = '<i class="icon-link-ext"></i>Open Folder in VolView</a>';
const openChecked = '<i class="icon-link-ext"></i>Open Checked in VolView</a>';

function loadResources(parentModel, resources) {
    // update lastOpened so manifest endpoint opens it
    const itemId =
        resources.item && resources.item.length >= 1 && resources.item[0];
    const folderId =
        resources.folder && resources.folder.length >= 1 && resources.folder[0];
    const id = itemId || folderId;
    const model = (itemId && "item") || (folderId && "folder");

    if (model) {
        restRequest({
            url: `${model}/${id}/metadata`,
            method: "PUT",
            contentType: "application/json",
            data: JSON.stringify({ lastOpened: new Date() }),
            error: null,
        }).done(() => {
            openResources(parentModel, resources);
        });
    } else {
        openResources(parentModel, resources);
    }
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
                if (
                    items.length >= 2 ||
                    (resources.folder && resources.folder.length >= 1)
                ) {
                    // Only newest checked volview.zip item will be opened, so warn.
                    confirm({
                        text: `Will open newest VolView zip file: ${volViewZip.attributes.name}.`,
                        yesText: "Open",
                        confirmCallback: () => {
                            const volViewResources = { item: [volViewZip.id] };
                            loadResources(this.parentModel, volViewResources);
                        },
                    });
                    return;
                } else {
                    const volViewResources = { item: [volViewZip.id] };
                    loadResources(this.parentModel, volViewResources);
                    return;
                }
            }
        }
        loadResources(this.parentModel, resources);
    };

    const updateChecked = () => {
        const resources = this._getCheckedResourceParam();
        button.innerHTML = resources.length >= 3 ? openChecked : openFolder;
    };
    updateChecked();

    this.listenTo(this.itemListView, "g:checkboxesChanged", updateChecked);
    this.listenTo(this.folderListView, "g:checkboxesChanged", updateChecked);
});
