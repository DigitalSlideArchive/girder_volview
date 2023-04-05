const origin = globalThis.location.origin;
const volViewPath = `${origin}/static/built/plugins/volview/index.html`;
const fileRoot = `${origin}/api/v1/item`;

export function open(model) {
    const downloadUrl = `${volViewPath}?names=[${model.name()}]&urls=[${fileRoot}/${
        model.id
    }/download]`;
    window.open(downloadUrl, "_blank").focus();
}
