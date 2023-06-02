const path = require("path");

const CopyWebpackPlugin = require("copy-webpack-plugin");

module.exports = function (config) {
    config.plugins.push(
        new CopyWebpackPlugin([
            {
                from: path.join(
                    path.resolve(__dirname),
                    "node_modules",
                    "volview-girder-client",
                    "VolView",
                    "dist"
                ),
                to: config.output.path,
                toType: "dir",
            },
        ])
    );
    return config;
};
