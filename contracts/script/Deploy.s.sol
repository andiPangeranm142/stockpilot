// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../src/StockPilotLedger.sol";

/// @notice Deploys StockPilotLedger to Base Sepolia (or any target network).
/// @dev Reads the deployer key from the DEPLOYER_PRIVATE_KEY environment
///      variable — never hardcode it. See contracts/README.md for the
///      exact command.
contract Deploy is Script {
    function run() external returns (StockPilotLedger) {
        uint256 deployerKey = vm.envUint("DEPLOYER_PRIVATE_KEY");

        vm.startBroadcast(deployerKey);
        StockPilotLedger ledger = new StockPilotLedger();
        vm.stopBroadcast();

        console.log("StockPilotLedger deployed at:", address(ledger));
        console.log("Owner (deployer):", ledger.owner());
        return ledger;
    }
}
