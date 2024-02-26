import HierarchyWidget from "@girder/core/views/widgets/HierarchyWidget";
import { wrap } from "@girder/core/utilities/PluginUtils";
import { openButton, openResources } from "./open";
import { restRequest } from "@girder/core/rest";

wrap(HierarchyWidget, "render", function (render) {
    render.call(this);

    this.$(".g-folder-header-buttons").prepend(openButton);
    const buttons = this.$el.find(".open-in-volview");

    buttons[0].onclick = () => {
        const resources = JSON.parse(this._getCheckedResourceParam());
        openResources(this.parentModel, resources);
    };
});
