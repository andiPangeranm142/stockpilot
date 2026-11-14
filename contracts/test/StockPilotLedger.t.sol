// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import "../src/StockPilotLedger.sol";

contract StockPilotLedgerTest is Test {
    StockPilotLedger public ledger;
    address public stranger = address(0xBEEF);

    bytes32 constant NAV_HASH = bytes32(uint256(0xAAAA));
    bytes32 constant RATIONALE_HASH = bytes32(uint256(0xBBBB));

    function setUp() public {
        ledger = new StockPilotLedger();
    }

    function _weights3() internal pure returns (uint256[] memory) {
        uint256[] memory w = new uint256[](3);
        w[0] = 4000;
        w[1] = 3500;
        w[2] = 2500;
        return w;
    }

    /*//////////////////////////////////////////////////////////////
                              HAPPY PATH
    //////////////////////////////////////////////////////////////*/

    function test_RecordFirstRebalance_SuccessAndEventAndStruct() public {
        uint256[] memory w = _weights3();

        vm.expectEmit(true, true, false, true, address(ledger));
        emit StockPilotLedger.RebalanceRecorded(
            0, address(this), block.timestamp, NAV_HASH, RATIONALE_HASH, "NVDAc,AAPLc,TSLA", w
        );

        uint256 id = ledger.recordRebalance(NAV_HASH, RATIONALE_HASH, "NVDAc,AAPLc,TSLA", w);
        assertEq(id, 0);
        assertEq(ledger.rebalanceCount(), 1);

        StockPilotLedger.Rebalance memory r = ledger.getRebalance(0);
        assertEq(r.id, 0);
        assertEq(r.timestamp, block.timestamp);
        assertEq(r.navSnapshotHash, NAV_HASH);
        assertEq(r.rationaleHash, RATIONALE_HASH);
        assertEq(r.tickersCsv, "NVDAc,AAPLc,TSLA");
        assertEq(r.weightsBps.length, 3);
        assertEq(r.weightsBps[0], 4000);
        assertEq(r.weightsBps[1], 3500);
        assertEq(r.weightsBps[2], 2500);
        assertEq(r.agent, address(this));
    }

    function test_SecondRecord_IdIncrementsAndCountCorrect() public {
        ledger.recordRebalance(NAV_HASH, RATIONALE_HASH, "NVDAc,AAPLc,TSLA", _weights3());

        uint256[] memory w2 = new uint256[](2);
        w2[0] = 6000;
        w2[1] = 4000;
        uint256 id2 = ledger.recordRebalance(bytes32(uint256(0x1111)), bytes32(uint256(0x2222)), "NVDAc,TSLA", w2);

        assertEq(id2, 1);
        assertEq(ledger.rebalanceCount(), 2);

        StockPilotLedger.Rebalance memory latest = ledger.getLatest();
        assertEq(latest.id, 1);
        assertEq(latest.tickersCsv, "NVDAc,TSLA");
        assertEq(latest.weightsBps[0], 6000);

        // First record is untouched.
        StockPilotLedger.Rebalance memory first = ledger.getRebalance(0);
        assertEq(first.tickersCsv, "NVDAc,AAPLc,TSLA");
    }

    function test_GetLatest_ReturnsMostRecent() public {
        ledger.recordRebalance(NAV_HASH, RATIONALE_HASH, "NVDAc,AAPLc,TSLA", _weights3());
        uint256[] memory w2 = new uint256[](1);
        w2[0] = 10000;
        ledger.recordRebalance(NAV_HASH, RATIONALE_HASH, "NVDAc", w2);

        StockPilotLedger.Rebalance memory latest = ledger.getLatest();
        assertEq(latest.id, 1);
        assertEq(latest.tickersCsv, "NVDAc");
    }

    /*//////////////////////////////////////////////////////////////
                              ACCESS CONTROL
    //////////////////////////////////////////////////////////////*/

    function test_OnlyOwnerCanRecord() public {
        vm.prank(stranger);
        vm.expectRevert("NotOwner");
        ledger.recordRebalance(NAV_HASH, RATIONALE_HASH, "NVDAc,AAPLc,TSLA", _weights3());
    }

    /*//////////////////////////////////////////////////////////////
                              VALIDATION REVERTS
    //////////////////////////////////////////////////////////////*/

    function test_RevertWhen_WeightsNot10000() public {
        uint256[] memory w = new uint256[](3);
        w[0] = 4000;
        w[1] = 3500;
        w[2] = 2000; // total 9500
        vm.expectRevert("WeightsNot10000");
        ledger.recordRebalance(NAV_HASH, RATIONALE_HASH, "NVDAc,AAPLc,TSLA", w);
    }

    function test_RevertWhen_TickerCountMismatchWeights() public {
        uint256[] memory w = new uint256[](2);
        w[0] = 5000;
        w[1] = 5000;
        vm.expectRevert("TickerWeightMismatch");
        ledger.recordRebalance(NAV_HASH, RATIONALE_HASH, "NVDAc,AAPLc,TSLA", w); // 3 tickers, 2 weights
    }

    function test_RevertWhen_ZeroHoldings() public {
        uint256[] memory w = new uint256[](0);
        vm.expectRevert("NoHoldings");
        ledger.recordRebalance(NAV_HASH, RATIONALE_HASH, "", w);
    }

    function test_RevertWhen_MoreThan20Holdings() public {
        uint256[] memory w = new uint256[](21);
        for (uint256 i = 0; i < 21; i++) {
            w[i] = 476;
        }
        vm.expectRevert("TooManyHoldings");
        ledger.recordRebalance(
            NAV_HASH,
            RATIONALE_HASH,
            "T1,T2,T3,T4,T5,T6,T7,T8,T9,T10,T11,T12,T13,T14,T15,T16,T17,T18,T19,T20,T21",
            w
        );
    }

    function test_RevertWhen_ZeroWeight() public {
        uint256[] memory w = new uint256[](2);
        w[0] = 10000;
        w[1] = 0;
        vm.expectRevert("ZeroWeight");
        ledger.recordRebalance(NAV_HASH, RATIONALE_HASH, "NVDAc,TSLA", w);
    }

    /*//////////////////////////////////////////////////////////////
                              GETTER REVERTS
    //////////////////////////////////////////////////////////////*/

    function test_RevertWhen_GetRebalanceOutOfRange() public {
        vm.expectRevert("RebalanceNotFound");
        ledger.getRebalance(0); // empty ledger

        ledger.recordRebalance(NAV_HASH, RATIONALE_HASH, "NVDAc,AAPLc,TSLA", _weights3());
        vm.expectRevert("RebalanceNotFound");
        ledger.getRebalance(1); // only id 0 exists
    }

    function test_RevertWhen_GetLatestOnEmptyLedger() public {
        vm.expectRevert("NoRebalances");
        ledger.getLatest();
    }
}
