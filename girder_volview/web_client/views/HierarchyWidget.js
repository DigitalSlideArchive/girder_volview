import HierarchyWidget from "@girder/core/views/widgets/HierarchyWidget";
import ItemListWidget from "@girder/large_image/views/itemList";
import { restRequest } from "@girder/core/rest";
import { confirm } from "@girder/core/dialog";
import { wrap } from "@girder/core/utilities/PluginUtils";

import {
    addButton,
    groupingFilterForItem,
    openCheckedGrouped,
    openCheckedGroupedURL,
    openGroupedItemURL,
    openItemURL,
    openResources,
    openResourcesURL,
} from "./open";

const openFolder = '<i class="icon-link-ext"></i>Open Folder in VolView</a>';
const openChecked = '<i class="icon-link-ext"></i>Open Checked in VolView</a>';

function setButtonVisibility(button, visible = true) {
    if (button.length === 0) {
        return;
    }
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

function isGroupedItemList(itemListView) {
    if (!itemListView || typeof itemListView._confList !== "function") {
        return false;
    }
    const conf = itemListView._confList();
    return !!(conf && conf.group && conf.group.keys && conf.group.keys.length);
}

function checkedGroupingFilters(itemListView, resources) {
    if (!isGroupedItemList(itemListView)) {
        return null;
    }
    const ids = (resources && resources.item) || [];
    const filters = ids
        .map((cid) => itemListView.collection.get(cid))
        .filter((model) => model && (model.get("meta") || {})._grouping)
        .map((model) => groupingFilterForItem(model))
        .filter((f) => Object.keys(f).length > 0);
    return filters.length ? filters : null;
}

function isSessionItem(item) {
    const name = item.attributes.name;
    return name.includes(".volview.zip") || name.includes(".volview.json");
}

wrap(HierarchyWidget, "render", function (render) {
    render.call(this);

    if (
        !this._showActions ||
        // Can't open/save at the root of Collections.
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

        const groupedFilters = checkedGroupingFilters(this.itemListView, resources);
        if (groupedFilters) {
            openCheckedGrouped(this.parentModel, groupedFilters);
            return false;
        }

        if (resources.item && resources.item.length > 0) {
            const items = resources.item.map((cid) =>
                this.itemListView.collection.get(cid),
            );
            const volViewZipsNewestFirst = items
                .filter(isSessionItem)
                .sort(
                    (a, b) =>
                        new Date(b.attributes.created) -
                        new Date(a.attributes.created),
                );

            if (volViewZipsNewestFirst.length > 0) {
                const volViewZip = volViewZipsNewestFirst[0];
                const volViewResources = { item: [volViewZip.id] };
                if (
                    items.length >= 2 ||
                    (resources.folder && resources.folder.length >= 1)
                ) {
                    confirm({
                        text: `Will open newest VolView session: ${volViewZip.attributes.name}.`,
                        yesText: "Open",
                        confirmCallback: () => {
                            openResources(this.parentModel, volViewResources);
                        },
                    });
                    return false;
                }
                openResources(this.parentModel, volViewResources);
                return false;
            }
        }
        openResources(this.parentModel, resources);
        return false;
    };

    const updateChecked = () => {
        const resourceParam = this._getCheckedResourceParam();
        const resources = JSON.parse(resourceParam);
        const groupedFilters = checkedGroupingFilters(this.itemListView, resources);
        if (groupedFilters) {
            button.innerHTML = openChecked;
            $(button).attr("href", openCheckedGroupedURL(this.parentModel, groupedFilters));
            return;
        }
        const hasResources = (
            (resources.item && resources.item.length) ||
            (resources.folder && resources.folder.length)
        );
        button.innerHTML = hasResources ? openChecked : openFolder;
        $(button).attr("href", openResourcesURL(this.parentModel, resources));
    };
    updateChecked();

    this.listenTo(this.itemListView, "g:checkboxesChanged", updateChecked);
    this.listenTo(this.folderListView, "g:checkboxesChanged", updateChecked);

    updateButtonVisibility(
        this.$el.find(".open-in-volview"),
        this.parentModel.id,
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

    const id = this.collection.params.folderId;
    const button = this.$el
        .closest(".g-hierarchy-widget")
        .find(".open-in-volview");
    updateButtonVisibility(button, id);
});

ItemListWidget.registeredApplications.volview = {
    name: "VolView",
    check: (modelType, model, folder) => {
        if (modelType === "item") {
            if (isSessionItem(model)) {
                // A session.volview.zip/json item opens as a saved session.
            } else {
                try {
                    if (!model.get("meta") || !model.get("meta").dicom || model.get("meta").dicom.Modality === "SM") {
                        return false;
                    }
                } catch (e) {
                    return false;
                }
            }
            if (model.get("meta")._grouping) {
                return { url: openGroupedItemURL(model, folder) };
            }
            return { url: openItemURL(model) };
        }
        if (modelType === "folder") {
            // TODO: this needs to mimic what is done in python
            return { url: openResourcesURL(model, {}) };
        }
    },
};
