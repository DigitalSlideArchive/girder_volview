import HierarchyWidget from "@girder/core/views/widgets/HierarchyWidget";
import { wrap } from "@girder/core/utilities/PluginUtils";
import { openButton, openFiles } from "./open";
import { restRequest } from "@girder/core/rest";

wrap(HierarchyWidget, "render", function (render) {
    render.call(this);

    this.$(".g-folder-header-buttons .g-folder-info-button").before(openButton);
    const buttons = this.$el.find(".open-in-volview");

    buttons[0].onclick = () => {
        const resources = JSON.parse(this._getCheckedResourceParam());
        const items = resources.item.map((cid) =>
            this.itemListView.collection.get(cid)
        );
        Promise.all(
            items.map((item) => {
                return new Promise((resolve) => {
                    restRequest({
                        type: "GET",
                        url: `item/${item.id}/files?limit=0`,
                        error: null,
                    }).done((files) => {
                        resolve(files);
                    });
                });
            })
        ).then((files) => {
            const allFiles = files.flat();
            openFiles(this.parentModel, allFiles);
        });
    };
});
