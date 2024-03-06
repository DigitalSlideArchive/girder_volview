import { wrap } from "@girder/core/utilities/PluginUtils";
import ItemView from "@girder/core/views/body/ItemView";
import { openItem, openButton } from "./open";

const brandName = "VolView";

wrap(ItemView, "render", function (render) {
    this.once("g:rendered", function () {
        this.$el.find(".g-item-header .btn-group").before(openButton);
        const buttons = this.$el.find(".open-in-volview");
        buttons[0].onclick = () => openItem(this.model);
    });
    render.call(this);
});
