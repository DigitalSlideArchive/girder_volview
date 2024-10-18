import HierarchyWidget from "@girder/core/views/widgets/HierarchyWidget";
import ItemListWidget from "@girder/large_image/views/itemList";
import { restRequest } from "@girder/core/rest";
import { confirm } from "@girder/core/dialog";
import { wrap } from "@girder/core/utilities/PluginUtils";
import { addButton, openResources, openGroupedItemURL, openItemURL, openResourcesURL } from "./open";

const openFolder = '<i class="icon-link-ext"></i>Open Folder in VolView</a>';
const openChecked = '<i class="icon-link-ext"></i>Open Checked in VolView</a>';

function setButtonVisibility(button, visible = true) {
    if (button.length === 0) throw new Error("Button not found");
    if (visible) {
        button.removeClass("hidden");
    } else {
        button.addClass("hidden");
    }
}

function updateButtonVisibility(el, folderId) {
    restRequest({
        url: `folder/${folderId}/volview_loadable`,
        method: "GET",
        error: function () {
            setButtonVisibility(el, false);
        },
    }).done((loadableJSON) => {
        const loadable = loadableJSON.loadable;
        setButtonVisibility(el, loadable);
    });
}

function loadResources(parentModel, resources) {
    // update lastOpened so manifest endpoint opens checked resource
    // rather than newest session.volview.zip with matching resource set.
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

    if (
        !this._showActions ||
        // Can't open/save at root of Collections, for now.
        this.parentModel.attributes._modelType !== "folder"
    ) {
        return;
    }
    const button = addButton(this.$el, ".g-folder-header-buttons");
    if (!button) {
        return;
    }

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

    updateButtonVisibility(
        this.$el.find(".open-in-volview"),
        this.parentModel.id
    );
});

wrap(ItemListWidget, "render", function (render) {
    render.call(this);
    if (
        !this.collection ||
        !this.collection.models ||
        !this.collection.models.length
    ) {
        return;
    }

    // check if child folders/items have loadable files
    const id = this.collection.params.folderId;
    const button = this.$el
        .closest(".g-hierarchy-widget")
        .find(".open-in-volview");
    updateButtonVisibility(button, id);
});

ItemListWidget.registeredApplications['volview'] = {
    name: 'VolView',
    // icon:
    check: (modelType, model, folder) => {
        if (modelType === 'item') {
            if (model.get('name').endsWith('volview.zip')) {
                // use this
            } else {
                try {
                    if (!model.get('meta') || !model.get('meta').dicom || model.get('meta').dicom.Modality === 'SM') {
                        return false;
                    }
                } catch (e) {
                    return false;
                }
            }
            if (model.get('meta')._grouping) {
                return {url: openGroupedItemURL(model, folder)};
            }
            return {url: openItemURL(model)};
        }
        if (modelType === 'folder') {
            // TODO: this needs to mimic what is done in python
            return {url: openResourcesURL(model, {})};
        }
    }
};
