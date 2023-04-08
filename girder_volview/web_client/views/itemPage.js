import { wrap } from "@girder/core/utilities/PluginUtils";
import ItemView from "@girder/core/views/body/ItemView";
import { open } from "./open";

const brandName = "VolView";

wrap(ItemView, "render", function (render) {
    this.once("g:rendered", function () {
        this.$el.find(".g-item-header .btn-group").before(
            `<a class="btn btn-sm btn-primary open-in-volview" style="margin-left: 10px" role="button">
                <i class="icon-link-ext"></i>Open in ${brandName}
            </a>`
        );
        const buttons = this.$el.find(".open-in-volview");
        buttons[0].onclick = () => open(this.model);
    });
    render.call(this);
});
