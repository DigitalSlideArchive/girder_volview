import { wrap } from "@girder/core/utilities/PluginUtils";
import ItemView from "@girder/core/views/body/ItemView";
import { restRequest } from "@girder/core/rest";
import { openItem, addButton } from "./open";

function setupButton(el, model) {
    const button = addButton(el, ".g-item-header .btn-group");
    if (button) {
        el.find(".open-in-volview").removeClass("hidden");
        button.onclick = () => openItem(model);
    }
}

wrap(ItemView, "render", function (render) {
    this.once("g:rendered", function () {
        // check if item has loadable files
        const id = this.model.id;
        restRequest({
            url: `item/${id}/volview_loadable`,
            method: "GET",
            error: null,
        }).done((loadableJSON) => {
            if (loadableJSON.loadable) {
                setupButton(this.$el, this.model);
            }
        });
    });
    render.call(this);
});
