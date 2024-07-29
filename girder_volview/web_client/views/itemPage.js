import { wrap } from "@girder/core/utilities/PluginUtils";
import ItemView from "@girder/core/views/body/ItemView";
import { openItem, addButton, knownExtensions } from "./open";

wrap(ItemView, "render", function (render) {
    this.once("g:rendered", function () {
        const lastExt = this.model.get('name').split('.').slice(-1)[0].toLowerCase();
        if (!knownExtensions.includes(lastExt)) {
            return;
        }
        const button = addButton(this.$el, ".g-item-header .btn-group");
        if (button) {
            this.$el.find('.open-in-volview').removeClass('hidden');
            button.onclick = () => openItem(this.model);
        }
    });
    render.call(this);
});
