import { wrap } from "@girder/core/utilities/PluginUtils";
import ItemView from "@girder/core/views/body/ItemView";
import { restRequest } from "@girder/core/rest";
import { openItem, addButton, knownExtensions } from "./open";

function setupButton(el) {
    const button = addButton(el, ".g-item-header .btn-group");
    if (button) {
        el.find(".open-in-volview").removeClass("hidden");
        button.onclick = () => openItem(this.model);
    }
}

wrap(ItemView, "render", function (render) {
    this.once("g:rendered", function () {
        const lastExt = this.model
            .get("name")
            .split(".")
            .slice(-1)[0]
            .toLowerCase();
        if (knownExtensions.includes(lastExt)) {
            setupButton(this.$el);
            return;
        }

        // check if item has loadable files
        const id = this.model.id;
        restRequest({
            url: `item/${id}/volview_loadable`,
            method: "GET",
        }).done((loadableJSON) => {
            const loadable = loadableJSON.loadable;
            if (loadable) {
                setupButton(this.$el);
            }
        });
    });
    render.call(this);
});
