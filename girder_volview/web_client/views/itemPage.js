import { openItem, addButton } from "./open";

const { wrap } = girder.utilities.PluginUtils;

function setupButton(el, model) {
    const button = addButton(el, ".g-item-header .btn-group");
    if (button) {
        el.find(".open-in-volview").removeClass("hidden");
        button.onclick = () => openItem(model);
    }
}

wrap(girder.views.body.ItemView, "render", function (render) {
    this.once("g:rendered", function () {
        // check if item has loadable files
        const id = this.model.id;
        girder.rest.restRequest({
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
