import { wrap } from "@girder/core/utilities/PluginUtils";
import ItemView from "@girder/core/views/body/ItemView";

const origin = globalThis.location.origin;
const volViewPath = `${origin}/static/built/plugins/volview/index.html`;
const fileRoot = `${origin}/api/v1/item`;

const brandName = "VolView";

wrap(ItemView, "render", function (render) {
    this.once("g:rendered", function () {
        this.$el.find(".g-item-header .btn-group").before(
            `<a class="btn btn-sm btn-primary" style="margin-left: 10px" role="button" 
                href="${volViewPath}?names=[${this.model.name()}]&urls=[${fileRoot}/${
                this.model.id
            }/download]" target="_blank"
            >
                <i class="icon-link-ext"></i>Open in ${brandName}
            </a>`
        );
    });
    render.call(this);
});
