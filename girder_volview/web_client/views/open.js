import { restRequest } from "@girder/core/rest";

const origin = globalThis.location.origin;
const volViewPath = `${origin}/static/built/plugins/volview/index.html`;
const fileRoot = `${origin}/api/v1/item`;

function isSessionFile(fileName) {
    return fileName.endsWith("volview.zip");
}

function makeDownloadParams(model, itemRoot, files) {
    if (files.length === 0) return "";

    const hasSessionFiles = files.some(({ name }) => isSessionFile(name));
    const downloadUrl = hasSessionFiles
        ? `${itemRoot}/volview`
        : `${itemRoot}/volview/datasets`;

    return `&names=[${model.name()}.zip]&urls=[${downloadUrl}]`;
}

export function open(model) {
    restRequest({
        type: "GET",
        url: `item/${model.id}/files?limit=0`,
        error: null,
    })
        .done((files) => {
            const itemRoot = `${fileRoot}/${model.id}`;
            const saveUrl = `${itemRoot}/volview`;
            const downloadParams = makeDownloadParams(model, itemRoot, files);
            const newTabUrl = `${volViewPath}?save=${saveUrl}${downloadParams}`;
            window.open(newTabUrl, "_blank").focus();
        })
        .fail((resp) => {
            events.trigger("g:alert", {
                icon: "cancel",
                text: "Could not check files to open in VolView",
                type: "danger",
                timeout: 4000,
            });
        });
}
