import { wrap } from "@girder/core/utilities/PluginUtils";
import ItemView from "@girder/core/views/body/ItemView";
import { openItem, addButton } from "./open";

wrap(ItemView, "render", function (render) {
    this.once("g:rendered", function () {
        const button = addButton(this.$el, ".g-item-header .btn-group");
        if (button) {
            button.onclick = () => openItem(this.model);
        }
    });
    render.call(this);
});
